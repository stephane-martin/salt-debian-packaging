# -*- coding: utf-8 -*-
'''
Vultr Cloud Module using python-vultr bindings
==============================================

.. versionadded:: 2016.3.0

The Vultr cloud module is used to control access to the Vultr VPS system.

Use of this module only requires the ``api_key`` parameter.

Set up the cloud configuration at ``/etc/salt/cloud.providers`` or
``/etc/salt/cloud.providers.d/vultr.conf``:

.. code-block:: yaml

my-vultr-config:
  # Vultr account api key
  api_key: <supersecretapi_key>
  driver: vultr

'''

# Import python libs
from __future__ import absolute_import
import pprint
import logging
import time

# Import salt cloud libs
import salt.config as config
import salt.utils.cloud
from salt.exceptions import SaltCloudSystemExit

# Get logging started
log = logging.getLogger(__name__)

__virtualname__ = 'vultr'

DETAILS = {}


def __virtual__():
    '''
    Set up the Vultr functions and check for configurations
    '''
    if get_configured_provider() is False:
        return False

    return __virtualname__


def get_configured_provider():
    '''
    Return the first configured instance
    '''
    return config.is_provider_configured(
        __opts__,
        __active_provider_name__ or 'vultr',
        ('api_key',)
    )


def _cache_provider_details(conn=None):
    '''
    Provide a place to hang onto results of --list-[locations|sizes|images]
    so we don't have to go out to the API and get them every time.
    '''
    DETAILS['avail_locations'] = {}
    DETAILS['avail_sizes'] = {}
    DETAILS['avail_images'] = {}
    locations = avail_locations(conn)
    images = avail_images(conn)
    sizes = avail_sizes(conn)

    for key, location in locations.iteritems():
        DETAILS['avail_locations'][location['name']] = location
        DETAILS['avail_locations'][key] = location

    for key, image in images.iteritems():
        DETAILS['avail_images'][image['name']] = image
        DETAILS['avail_images'][key] = image

    for key, vm_size in sizes.iteritems():
        DETAILS['avail_sizes'][vm_size['name']] = vm_size
        DETAILS['avail_sizes'][key] = vm_size


def avail_locations(conn=None):
    '''
    return available datacenter locations
    '''
    return _query('regions/list')


def avail_sizes(conn=None):
    '''
    Return available sizes ("plans" in VultrSpeak)
    '''
    return _query('plans/list')


def avail_images(conn=None):
    '''
    Return available images
    '''
    return _query('os/list')


def list_nodes(**kwargs):
    '''
    Return basic data on nodes
    '''
    ret = {}

    nodes = list_nodes_full()
    for node in nodes:
        ret[node] = {}
        for prop in 'id', 'image', 'size', 'state', 'private_ips', 'public_ips':
            ret[node][prop] = nodes[node][prop]

    return ret


def list_nodes_full(**kwargs):
    '''
    Return all data on nodes
    '''
    nodes = _query('server/list')
    ret = {}

    for node in nodes:
        name = nodes[node]['label']
        ret[name] = nodes[node].copy()
        ret[name]['id'] = node
        ret[name]['image'] = nodes[node]['os']
        ret[name]['size'] = nodes[node]['VPSPLANID']
        ret[name]['state'] = nodes[node]['status']
        ret[name]['private_ips'] = nodes[node]['internal_ip']
        ret[name]['public_ips'] = nodes[node]['main_ip']

    return ret


def list_nodes_select(conn=None, call=None):
    '''
    Return a list of the VMs that are on the provider, with select fields
    '''
    return salt.utils.cloud.list_nodes_select(
        list_nodes_full(), __opts__['query.selection'], call,
    )


def destroy(name):
    '''
    Remove a node from Vultr
    '''
    node = show_instance(name, call='action')
    params = {'SUBID': node['SUBID']}
    result = _query('server/destroy', method='POST', decode=False, data=params)
    if result['body'] == '' and result['text'] == '':
        return True
    return result


def stop(*args, **kwargs):
    '''
    Execute a "stop" action on a VM
    '''
    return _query('server/halt')


def start(*args, **kwargs):
    '''
    Execute a "start" action on a VM
    '''
    return _query('server/start')


def show_instance(name, call=None):
    '''
    Show the details from the provider concerning an instance
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The show_instance action must be called with -a or --action.'
        )

    nodes = list_nodes_full()
    # Find under which cloud service the name is listed, if any
    if name not in nodes:
        return {}
    salt.utils.cloud.cache_node(nodes[name], __active_provider_name__, __opts__)
    return nodes[name]


def _lookup_vultrid(which_key, availkey, keyname):
    if DETAILS == {}:
        _cache_provider_details()

    which_key = str(which_key)
    try:
        return DETAILS[availkey][which_key][keyname]
    except KeyError:
        return False


def create(vm_):
    '''
    Create a single VM from a data dict
    '''
    if 'driver' not in vm_:
        vm_['driver'] = vm_['provider']

    salt.utils.cloud.fire_event(
        'event',
        'starting create',
        'salt/cloud/{0}/creating'.format(vm_['name']),
        {
            'name': vm_['name'],
            'profile': vm_['profile'],
            'provider': vm_['driver'],
        },
        transport=__opts__['transport']
    )

    osid = _lookup_vultrid(vm_['image'], 'avail_images', 'OSID')
    if not osid:
        log.error('Vultr does not have an image with id or name {0}'.format(vm_['image']))
        return False

    vpsplanid = _lookup_vultrid(vm_['size'], 'avail_sizes', 'VPSPLANID')
    if not vpsplanid:
        log.error('Vultr does not have a size with id or name {0}'.format(vm_['size']))
        return False

    dcid = _lookup_vultrid(vm_['location'], 'avail_locations', 'DCID')
    if not dcid:
        log.error('Vultr does not have a location with id or name {0}'.format(vm_['location']))
        return False

    kwargs = {
        'label': vm_['name'],
        'OSID': osid,
        'VPSPLANID': vpsplanid,
        'DCID': dcid,
    }

    log.info('Creating Cloud VM {0}'.format(vm_['name']))

    salt.utils.cloud.fire_event(
        'event',
        'requesting instance',
        'salt/cloud/{0}/requesting'.format(vm_['name']),
        {'kwargs': kwargs},
        transport=__opts__['transport'],
    )

    try:
        data = _query('server/create', method='POST', data=kwargs)
        if int(data.get('status', '200')) >= 300:
            log.error('Error creating {0} on Vultr\n\n'
                'Vultr API returned {1}\n'.format(vm_['name'], data))
            log.error('Status 412 may mean that you are requesting an\n'
                      'invalid location, image, or size.')

            salt.utils.cloud.fire_event(
                'event',
                'instance request failed',
                'salt/cloud/{0}/requesting/failed'.format(vm_['name']),
                {'kwargs': kwargs},
                transport=__opts__['transport'],
            )
            return False
    except Exception as exc:
        log.error(
            'Error creating {0} on Vultr\n\n'
            'The following exception was thrown when trying to '
            'run the initial deployment: \n{1}'.format(
                vm_['name'], str(exc)
            ),
            # Show the traceback if the debug logging level is enabled
            exc_info_on_loglevel=logging.DEBUG
        )
        salt.utils.cloud.fire_event(
            'event',
            'instance request failed',
            'salt/cloud/{0}/requesting/failed'.format(vm_['name']),
            {'kwargs': kwargs},
            transport=__opts__['transport'],
        )
        return False

    def wait_for_hostname():
        '''
        Wait for the IP address to become available
        '''
        data = show_instance(vm_['name'], call='action')
        pprint.pprint(data)
        if str(data.get('main_ip', '0')) == '0':
            time.sleep(3)
            return False
        return data['main_ip']

    def wait_for_default_password():
        '''
        Wait for the IP address to become available
        '''
        data = show_instance(vm_['name'], call='action')
        pprint.pprint(data)
        if str(data.get('default_password', '')) == '':
            time.sleep(1)
            return False
        if '!' not in data['default_password']:
            time.sleep(1)
            return False
        return data['default_password']

    vm_['ssh_host'] = salt.utils.cloud.wait_for_fun(
        wait_for_hostname,
        timeout=config.get_cloud_config_value(
            'wait_for_fun_timeout', vm_, __opts__, default=15 * 60),
    )
    vm_['password'] = salt.utils.cloud.wait_for_fun(
        wait_for_default_password,
        timeout=config.get_cloud_config_value(
            'wait_for_fun_timeout', vm_, __opts__, default=15 * 60),
    )
    __opts__['hard_timeout'] = config.get_cloud_config_value(
        'hard_timeout',
        get_configured_provider(),
        __opts__,
        search_global=False,
        default=15,
    )

    # Bootstrap
    ret = salt.utils.cloud.bootstrap(vm_, __opts__)

    ret.update(show_instance(vm_['name'], call='action'))

    log.info('Created Cloud VM \'{0[name]}\''.format(vm_))
    log.debug(
        '\'{0[name]}\' VM creation details:\n{1}'.format(
        vm_, pprint.pformat(data)
            )
    )

    salt.utils.cloud.fire_event(
        'event',
        'created instance',
        'salt/cloud/{0}/created'.format(vm_['name']),
        {
            'name': vm_['name'],
            'profile': vm_['profile'],
            'provider': vm_['driver'],
        },
        transport=__opts__['transport']
    )

    return ret


def _query(path, method='GET', data=None, params=None, header_dict=None, decode=True):
    '''
    Perform a query directly against the Vultr REST API
    '''
    api_key = config.get_cloud_config_value(
        'api_key',
        get_configured_provider(),
        __opts__,
        search_global=False,
    )
    management_host = config.get_cloud_config_value(
        'management_host',
        get_configured_provider(),
        __opts__,
        search_global=False,
        default='api.vultr.com'
    )
    url = 'https://{management_host}/v1/{path}?api_key={api_key}'.format(
        management_host=management_host,
        path=path,
        api_key=api_key,
    )

    if header_dict is None:
        header_dict = {}

    result = salt.utils.http.query(
        url,
        method=method,
        params=params,
        data=data,
        header_dict=header_dict,
        port=443,
        text=True,
        decode=decode,
        decode_type='json',
        hide_fields=['api_key'],
        opts=__opts__,
    )
    if 'dict' in result:
        return result['dict']

    return result
