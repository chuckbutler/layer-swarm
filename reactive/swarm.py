from charmhelpers.core.templating import render
from charms.reactive import remove_state
from charms.reactive import set_state
from charms.reactive import when
from charms.reactive import when_not

from charms.docker import DockerOpts
from charms.docker import Compose

from charmhelpers.core.hookenv import log
from charmhelpers.core.hookenv import status_set
from charmhelpers.core.hookenv import is_leader
from charmhelpers.core.hookenv import leader_get
from charmhelpers.core.hookenv import unit_get
from charmhelpers.core.hookenv import open_port
from charmhelpers.core.hookenv import unit_private_ip
from charmhelpers.core import unitdata
from charmhelpers.core.host import chdir
from charmhelpers.core.host import service_restart

from os import getenv
from os import makedirs
from os import path
from os import rename

import subprocess
from shlex import split
from shutil import copyfile

from tlslib import client_cert
from tlslib import ca


@when('etcd.available', 'docker.available')
@when_not('swarm.available')
def swarm_etcd_cluster_setup(etcd):
    """
    Expose the Docker TCP port, and begin swarm cluster configuration. Always
    leading with the agent, connecting to the discovery service, then follow
    up with the manager container on the leader node.
    """
    con_string = etcd.connection_string().replace('http', 'etcd')
    bind_docker_daemon(con_string)
    start_swarm(con_string)
    status_set('active', 'Swarm configured. Happy swarming')


@when('consul.available', 'docker.available')
@when_not('swarm.available')
def swarm_consul_cluster_setup(consul):
    connection_string = "consul://"
    for unit in consul.list_unit_data():
        host_string = "{}:{}".format(unit['address'], unit['port'])
        connection_string = "{}{},".format(connection_string, host_string)
    bind_docker_daemon(connection_string.rstrip(','))
    start_swarm(connection_string.rstrip(','))


def start_swarm(cluster_string):
    ''' Render the compose configuration and start the swarm scheduler '''
    opts = {}
    opts['addr'] = unit_private_ip()
    opts['port'] = 2376
    opts['leader'] = is_leader()
    opts['connection_string'] = cluster_string
    render('docker-compose.yml', 'files/swarm/docker-compose.yml', opts)
    c = Compose('files/swarm')
    c.up()
    set_state('swarm.available')


@when('swarm.available')
def swarm_messaging():
    if is_leader():
        status_set('active', 'Swarm leader running')
    else:
        status_set('active', 'Swarm follower')


@when_not('etcd.connected', 'consul.connected')
def user_notice():
    """
    Notify the user they need to relate the charm with ETCD or Consul to
    trigger the swarm cluster configuration.
    """
    status_set('waiting', 'Waiting on Etcd or Consul relation')


@when('swarm.available')
@when_not('etcd.connected', 'consul.connected')
def swarm_relation_broken():
    """
    Destroy the swarm agent, and optionally the manager.
    This state should only be entered if the Docker host relation with ETCD has
    been broken, thus leaving the cluster without a discovery service
    """
    c = Compose('files/swarm')
    c.kill()
    c.rm()
    remove_state('swarm.available')
    status_set('waiting', 'Reconfiguring swarm')


@when('easyrsa installed')
@when_not('swarm.tls.opensslconfig.modified')
def inject_swarm_tls_template():
    """
    layer-tls installs a default OpenSSL Configuration that is incompatibile
    with how swarm expects TLS keys to be generated. We will append what
    we need to the x509-type, and poke layer-tls to regenerate.
    """
    if not is_leader():
        return
    else:
        status_set('maintenance', 'Reconfiguring SSL PKI configuration')

    log('Updating EasyRSA3 OpenSSL Config')
    openssl_config = 'easy-rsa/easyrsa3/x509-types/server'

    with open(openssl_config, 'r') as f:
        existing_template = f.readlines()

    # use list comprehension to enable clients,server usage for certificates
    # with the docker/swarm daemons.
    xtype = [w.replace('serverAuth', 'serverAuth, clientAuth') for w in existing_template]  # noqa
    with open(openssl_config, 'w+') as f:
        f.writelines(xtype)

    set_state('swarm.tls.opensslconfig.modified')
    set_state('easyrsa configured')


@when('tls.server.certificate available')
def enable_client_tls():
    '''
    Copy the TLS certificates in place and generate mount points for the swarm
    manager to mount the certs. This enables client-side TLS security on the
    TCP service.
    '''
    if not path.exists('/etc/docker'):
        makedirs('/etc/docker')

    kv = unitdata.kv()
    cert = kv.get('tls.server.certificate')
    with open('/etc/docker/server.pem', 'w+') as f:
        f.write(cert)
    with open('/etc/docker/ca.pem', 'w+') as f:
        f.write(leader_get('certificate_authority'))

    # schenanigans
    keypath = 'easy-rsa/easyrsa3/pki/private/{}.key'
    server = getenv('JUJU_UNIT_NAME').replace('/', '_')
    if path.exists(keypath.format(server)):
        copyfile(keypath.format(server), '/etc/docker/server-key.pem')
    else:
        copyfile(keypath.format(unit_get('public-address')),
                 '/etc/docker/server-key.pem')

    opts = DockerOpts()
    config_dir = '/etc/docker'
    cert_path = '{}/server.pem'.format(config_dir)
    ca_path = '{}/ca.pem'.format(config_dir)
    key_path = '{}/server-key.pem'.format(config_dir)
    opts.add('tlscert', cert_path)
    opts.add('tlscacert', ca_path)
    opts.add('tlskey', key_path)
    opts.add('tlsverify', None)
    render('docker.defaults', '/etc/default/docker', {'opts': opts.to_s()})


@when('swarm.available')
@when_not('client.credentials.placed')
def prepare_end_user_package():
    """ Generate a downloadable package for clients to use to speak to the
    swarm cluster. """
    if is_leader():
        client_cert('./swarm_credentials')
        ca('./swarm_credentials')

        # Prepare the workspace
        with chdir('./swarm_credentials'):
            rename('client.key', 'key.pem')
            rename('client.crt', 'cert.pem')
            rename('ca.crt', 'ca.pem')

        template_vars = {'public_address': unit_get('public-address')}

        render('enable.sh', './swarm_credentials/enable.sh', template_vars)

        cmd = 'tar cvf swarm_credentials.tar swarm_credentials'
        subprocess.check_call(split(cmd))
        copyfile('swarm_credentials.tar', '/home/ubuntu/swarm_credentials.tar')
        set_state('client.credentials.placed')


def bind_docker_daemon(connection_string):
    status_set('maintenance', 'Configuring Docker for TCP connections')
    opts = DockerOpts()
    private_address = unit_private_ip()
    opts.add('host', 'tcp://{}:2376'.format(private_address))
    opts.add('host', 'unix:///var/run/docker.sock')
    opts.add('cluster-advertise', '{}:2376'.format(private_address))
    opts.add('cluster-store', connection_string, strict=True)
    render('docker.defaults', '/etc/default/docker', {'opts': opts.to_s()})
    service_restart('docker')
    open_port(2376)
    if is_leader():
        open_port(3376)
