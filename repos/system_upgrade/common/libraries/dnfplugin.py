import contextlib
import itertools
import json
import os
import shutil

from leapp.exceptions import StopActorExecutionError
from leapp.libraries.common import dnfconfig, guards, mounting, overlaygen, rhsm, utils
from leapp.libraries.common.config.version import get_target_major_version, get_source_major_version
from leapp.libraries.stdlib import CalledProcessError, api, config


DNF_PLUGIN_NAME = 'rhel_upgrade.py'


class _DnfPluginPathStr(str):
    _PATHS = {
        "8": os.path.join('/lib/python3.6/site-packages/dnf-plugins', DNF_PLUGIN_NAME),
        "9": os.path.join('/lib/python3.9/site-packages/dnf-plugins', DNF_PLUGIN_NAME),
    }

    def __init__(self):  # noqa: W0231; pylint: disable=super-init-not-called
        self.data = ""

    def _feed(self):
        major = get_target_major_version()
        if major not in _DnfPluginPathStr._PATHS.keys():  # pylint: disable=W1655
            raise KeyError('{} is not a supported target version of RHEL'.format(major))
        self.data = _DnfPluginPathStr._PATHS[major]

    def __str__(self):
        self._feed()
        return str(self.data)

    def __repr__(self):
        self._feed()
        return repr(self.data)

    def lstrip(self, chars=None):
        self._feed()
        return self.data.lstrip(chars)


# Deprecated
DNF_PLUGIN_PATH = _DnfPluginPathStr()

DNF_PLUGIN_DATA_NAME = 'dnf-plugin-data.txt'
DNF_PLUGIN_DATA_PATH = os.path.join('/var/lib/leapp', DNF_PLUGIN_DATA_NAME)
DNF_PLUGIN_DATA_LOG_PATH = os.path.join('/var/log/leapp', DNF_PLUGIN_DATA_NAME)
DNF_DEBUG_DATA_PATH = '/var/log/leapp/dnf-debugdata/'


def install(target_basedir):
    """
    Installs our plugin to the DNF plugins.
    """
    try:
        shutil.copy2(
            api.get_file_path(DNF_PLUGIN_NAME),
            os.path.join(target_basedir, DNF_PLUGIN_PATH.lstrip('/')))
    except EnvironmentError as e:
        api.current_logger().debug('Failed to install DNF plugin', exc_info=True)
        raise StopActorExecutionError(
            message='Failed to install DNF plugin. Error: {}'.format(str(e))
        )


def _rebuild_rpm_db(context, root=None):
    """
    Convert rpmdb from BerkeleyDB to Sqlite
    """
    base_cmd = ['rpmdb', '--rebuilddb']
    cmd = base_cmd if not root else base_cmd + ['-r', root]
    context.call(cmd)


def build_plugin_data(target_repoids, debug, test, tasks, on_aws):
    """
    Generates a dictionary with the DNF plugin data.
    """
    # get list of repo IDs of target repositories that should be used for upgrade
    data = {
        'pkgs_info': {
            'local_rpms': [os.path.join('/installroot', pkg.lstrip('/')) for pkg in tasks.local_rpms],
            'to_install': tasks.to_install,
            'to_remove': tasks.to_remove,
            'to_upgrade': tasks.to_upgrade
        },
        'dnf_conf': {
            'allow_erasing': True,
            'best': True,
            'debugsolver': debug,
            'disable_repos': True,
            'enable_repos': target_repoids,
            'gpgcheck': False,
            'platform_id': 'platform:el{}'.format(get_target_major_version()),
            'releasever': api.current_actor().configuration.version.target,
            'installroot': '/installroot',
            'test_flag': test
        },
        'rhui': {
            'aws': {
              'on_aws': on_aws,
              'region': None,
            }
        }
    }
    return data


def create_config(context, target_repoids, debug, test, tasks, on_aws=False):
    """
    Creates the configuration data file for our DNF plugin.
    """
    context.makedirs(os.path.dirname(DNF_PLUGIN_DATA_PATH), exists_ok=True)
    with context.open(DNF_PLUGIN_DATA_PATH, 'w+') as f:
        config_data = build_plugin_data(
            target_repoids=target_repoids, debug=debug, test=test, tasks=tasks, on_aws=on_aws
        )
        json.dump(config_data, f, sort_keys=True, indent=2)


def backup_config(context):
    """
    Backs up the configuration data used for the plugin.
    """
    context.copy_from(DNF_PLUGIN_DATA_PATH, DNF_PLUGIN_DATA_LOG_PATH)


def backup_debug_data(context):
    """
    Performs the backup of DNF debug data
    """
    if config.is_debug():
        # The debugdata is a folder generated by dnf when using the --debugsolver dnf option. We switch on the
        # debug_solver dnf config parameter in our rhel-upgrade dnf plugin when LEAPP_DEBUG env var set to 1.
        try:
            context.copytree_from('/debugdata', DNF_DEBUG_DATA_PATH)
        except OSError as e:
            api.current_logger().warning('Failed to copy debugdata. Message: {}'.format(str(e)), exc_info=True)


def _transaction(context, stage, target_repoids, tasks, plugin_info, test=False, cmd_prefix=None, on_aws=False):
    """
    Perform the actual DNF rpm download via our DNF plugin
    """

    # we do not want
    if stage != 'upgrade':
        create_config(
            context=context,
            target_repoids=target_repoids,
            debug=config.is_debug(),
            test=test, tasks=tasks,
            on_aws=on_aws
        )
    backup_config(context=context)

    # FIXME: rhsm
    with guards.guarded_execution(guards.connection_guard(), guards.space_guard()):
        cmd = [
            '/usr/bin/dnf',
            'rhel-upgrade',
            stage,
            DNF_PLUGIN_DATA_PATH
        ]
        if config.is_verbose():
            cmd.append('-v')
        if rhsm.skip_rhsm():
            cmd += ['--disableplugin', 'subscription-manager']
        if plugin_info:
            for info in plugin_info:
                if stage in info.disable_in:
                    cmd += ['--disableplugin', info.name]
        if cmd_prefix:
            cmd = cmd_prefix + cmd
        env = {}
        if get_target_major_version() == '9':
            # allow handling new RHEL 9 syscalls by systemd-nspawn
            env = {'SYSTEMD_SECCOMP': '0'}
        try:
            context.call(
                cmd=cmd,
                callback_raw=utils.logging_handler,
                env=env
            )
        except OSError as e:
            api.current_logger().error('Could not call dnf command: Message: %s', str(e), exc_info=True)
            raise StopActorExecutionError(
                message='Failed to execute dnf. Reason: {}'.format(str(e))
            )
        except CalledProcessError as e:
            api.current_logger().error('DNF execution failed: ')
            raise StopActorExecutionError(
                message='DNF execution failed with non zero exit code.\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}'.format(
                    stdout=e.stdout, stderr=e.stderr)
            )
        finally:
            if stage == 'check':
                backup_debug_data(context=context)


@contextlib.contextmanager
def _prepare_transaction(used_repos, target_userspace_info, binds=()):
    """ Creates the transaction environment needed for the target userspace DNF execution  """
    target_repoids = set()
    for message in used_repos:
        target_repoids.update([repo.repoid for repo in message.repos])
    with mounting.NspawnActions(base_dir=target_userspace_info.path, binds=binds) as context:
        yield context, list(target_repoids), target_userspace_info


def _apply_yum_workaround(context):
    """
    Apply the yum workaround in the given context environment if on RHEL 7.
    """
    if get_source_major_version() == '7':
        utils.apply_yum_workaround(context)


def install_initramdisk_requirements(packages, target_userspace_info, used_repos):
    """
    Performs the installation of packages into the initram disk
    """
    with _prepare_transaction(used_repos=used_repos,
                              target_userspace_info=target_userspace_info) as (context, target_repoids, _unused):
        if get_target_major_version() == '9':
            _rebuild_rpm_db(context)
        repos_opt = [['--enablerepo', repo] for repo in target_repoids]
        repos_opt = list(itertools.chain(*repos_opt))
        cmd = [
            'dnf',
            'install',
            '-y',
            '--nogpgcheck',
            '--setopt=module_platform_id=platform:el{}'.format(get_target_major_version()),
            '--setopt=keepcache=1',
            '--releasever', api.current_actor().configuration.version.target,
            '--disablerepo', '*'
        ] + repos_opt + list(packages)
        if config.is_verbose():
            cmd.append('-v')
        if rhsm.skip_rhsm():
            cmd += ['--disableplugin', 'subscription-manager']
        env = {}
        if get_target_major_version() == '9':
            # allow handling new RHEL 9 syscalls by systemd-nspawn
            env = {'SYSTEMD_SECCOMP': '0'}
        context.call(cmd, env=env)


def perform_transaction_install(target_userspace_info, storage_info, used_repos, tasks, plugin_info):
    """
    Performs the actual installation with the DNF rhel-upgrade plugin using the target userspace
    """

    # These bind mounts are performed by systemd-nspawn --bind parameters
    bind_mounts = [
        '/:/installroot',
        '/dev:/installroot/dev',
        '/proc:/installroot/proc',
        '/run/udev:/installroot/run/udev',
    ]

    if get_target_major_version() == '8':
        bind_mounts.append('/sys:/installroot/sys')
    else:
        # the target major version is RHEL 9+
        # we are bindmounting host's "/sys" to the intermediate "/hostsys"
        # in the upgrade initramdisk to avoid cgroups tree layout clash
        bind_mounts.append('/hostsys:/installroot/sys')

    already_mounted = {entry.split(':')[0] for entry in bind_mounts}
    for entry in storage_info.fstab:
        mp = entry.fs_file
        if not os.path.isdir(mp):
            continue
        if mp not in already_mounted:
            bind_mounts.append('{}:{}'.format(mp, os.path.join('/installroot', mp.lstrip('/'))))

    if os.path.ismount('/boot'):
        bind_mounts.append('/boot:/installroot/boot')

    if os.path.ismount('/boot/efi'):
        bind_mounts.append('/boot/efi:/installroot/boot/efi')

    with _prepare_transaction(used_repos=used_repos,
                              target_userspace_info=target_userspace_info,
                              binds=bind_mounts
                              ) as (context, target_repoids, _unused):
        # the below nsenter command is important as we need to enter sysvipc namespace on the host so we can
        # communicate with udev
        cmd_prefix = ['nsenter', '--ipc=/installroot/proc/1/ns/ipc']

        # we have to ensure the leapp packages will stay untouched
        # Note: this is the most probably duplicate action - it should be already
        # set like that, however seatbelt is a good thing.
        dnfconfig.exclude_leapp_rpms(context)

        if get_target_major_version() == '9':
            _rebuild_rpm_db(context, root='/installroot')
        _transaction(
            context=context, stage='upgrade', target_repoids=target_repoids, plugin_info=plugin_info, tasks=tasks,
            cmd_prefix=cmd_prefix
        )

        # we have to ensure the leapp packages will stay untouched even after the
        # upgrade is fully finished (it cannot be done before the upgarde
        # on the host as the config-manager plugin is available since rhel-8)
        dnfconfig.exclude_leapp_rpms(mounting.NotIsolatedActions(base_dir='/'))


def perform_transaction_check(target_userspace_info, used_repos, tasks, xfs_info, storage_info, plugin_info):
    """
    Perform DNF transaction check using our plugin
    """
    with _prepare_transaction(used_repos=used_repos,
                              target_userspace_info=target_userspace_info
                              ) as (context, target_repoids, userspace_info):
        with overlaygen.create_source_overlay(mounts_dir=userspace_info.mounts, scratch_dir=userspace_info.scratch,
                                              xfs_info=xfs_info, storage_info=storage_info,
                                              mount_target=os.path.join(context.base_dir, 'installroot')) as overlay:
            _apply_yum_workaround(overlay.nspawn())
            dnfconfig.exclude_leapp_rpms(context)
            _transaction(
                context=context, stage='check', target_repoids=target_repoids, plugin_info=plugin_info, tasks=tasks
            )


def perform_rpm_download(target_userspace_info, used_repos, tasks, xfs_info, storage_info, plugin_info, on_aws=False):
    """
    Perform RPM download including the transaction test using dnf with our plugin
    """
    with _prepare_transaction(used_repos=used_repos,
                              target_userspace_info=target_userspace_info
                              ) as (context, target_repoids, userspace_info):
        with overlaygen.create_source_overlay(mounts_dir=userspace_info.mounts, scratch_dir=userspace_info.scratch,
                                              xfs_info=xfs_info, storage_info=storage_info,
                                              mount_target=os.path.join(context.base_dir, 'installroot')) as overlay:
            _apply_yum_workaround(overlay.nspawn())
            dnfconfig.exclude_leapp_rpms(context)
            _transaction(
                context=context, stage='download', target_repoids=target_repoids, plugin_info=plugin_info, tasks=tasks,
                test=True, on_aws=on_aws
            )
