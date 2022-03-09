import apscheduler.schedulers.blocking
import boto3
import botocore.exceptions
import configparser
import enum
import logging
import os
import pathlib
import shlex
import signal
import subprocess
import sys
import time

_version = '2021.0'

log = logging.getLogger('deploy-chef-client')


class DeploymentResult(enum.Enum):
    BOOTSTRAP_FAILURE = enum.auto()
    BOOTSTRAP_SUCCESS = enum.auto()
    CHEF_NODE_EXISTS = enum.auto()
    CHEF_NODE_MISSING = enum.auto()
    EXCLUDED_WITH_TAG = enum.auto()
    INSTANCE_STATE_PENDING = enum.auto()
    INSTANCE_STATE_SHUTTING_DOWN = enum.auto()
    INSTANCE_STATE_STOPPED = enum.auto()
    INSTANCE_STATE_STOPPING = enum.auto()
    INSTANCE_STATE_TERMINATED = enum.auto()
    KEYFILE_MISSING = enum.auto()
    KEYFILE_UNKNOWN = enum.auto()
    PLATFORM_IS_LINUX = enum.auto()
    PLATFORM_IS_WINDOWS = enum.auto()
    SKIPPED = enum.auto()
    VPC_IGNORED = enum.auto()

    def report_details(self) -> bool:
        return self in (DeploymentResult.BOOTSTRAP_FAILURE, DeploymentResult.BOOTSTRAP_SUCCESS)


class Settings:
    @staticmethod
    def as_bool(value: str) -> bool:
        return value.lower() in ('true', 'yes', 'on', '1')

    @staticmethod
    def as_int(value: str, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @property
    def keyfile_location(self) -> pathlib.Path:
        return pathlib.Path(os.getenv('KEYFILE_LOCATION', 'keys')).resolve()

    @property
    def log_format(self) -> str:
        return os.getenv('LOG_FORMAT', '%(levelname)s [%(name)s] %(message)s')

    @property
    def log_level(self) -> str:
        return os.getenv('LOG_LEVEL', 'INFO')

    @property
    def other_log_levels(self) -> dict:
        result = {}
        for log_spec in os.getenv('OTHER_LOG_LEVELS', '').split():
            logger, _, level = log_spec.partition(':')
            result[logger] = level
        return result

    @property
    def run_and_exit(self) -> bool:
        return self.as_bool(os.getenv('RUN_AND_EXIT', 'false'))

    @property
    def run_interval(self) -> int:
        return self.as_int(os.getenv('RUN_INTERVAL'), 60)


def bootstrap_node_linux(hostname: str, user: str, node_name: str, ssh_identity_file: pathlib.Path):
    bootstrap_cmd = [
        'knife', 'bootstrap', hostname,
        '--bootstrap-preinstall-command', 'rm -f /etc/chef/client.pem',
        '--connection-protocol', 'ssh',
        '--connection-user', user,
        # '--no-color',
        '--node-name', node_name,
        '--run-list', 'recipe[chef_client_schedule]',
        '--session-timeout', '110',
        '--ssh-identity-file', str(ssh_identity_file),
        '--ssh-verify-host-key', 'never',
        '--sudo',
    ]
    log.info(f'# {shlex.join(bootstrap_cmd)}')

    # Keep trying for 5 minutes
    start_time = time.monotonic()
    while time.monotonic() < start_time + 300:
        elapsed_time = int(time.monotonic() - start_time)
        log.info(f'Tried for {elapsed_time}/300 seconds so far.')
        try:
            subprocess.run(bootstrap_cmd, check=True)
            return DeploymentResult.BOOTSTRAP_SUCCESS
        except subprocess.CalledProcessError:
            log.error('CalledProcessError')
        except subprocess.TimeoutExpired:
            log.error('TimeoutExpired')
    input('Press <Enter> to continue ...')
    return DeploymentResult.BOOTSTRAP_FAILURE


def bootstrap_node_windows(hostname: str, node_name: str):
    passwords_file = Settings().keyfile_location / 'windows-passwords.ini'
    passwords_config = configparser.ConfigParser(interpolation=None)
    passwords_config.read(passwords_file)
    passwords = passwords_config['passwords']
    log.info(f'Bootstrapping {node_name} / {hostname}')
    pw = passwords.get(node_name)
    if pw is None:
        pw = input('Enter Administrator password (or n to cancel): ')
        if pw == 'n':
            return DeploymentResult.SKIPPED
        passwords[node_name] = pw
        with passwords_file.open('w') as f:
            passwords_config.write(f)
    else:
        log.info(f'Using Administrator password from {passwords_file}')

    bootstrap_cmd = [
        'knife', 'bootstrap', hostname,
        '--connection-password', pw,
        '--connection-protocol', 'winrm',
        '--connection-user', 'Administrator',
        '--node-name', node_name,
        '--run-list', 'recipe[chef_client_schedule]',
        '--session-timeout', '110',
    ]

    log.info(f'# {shlex.join(bootstrap_cmd)}')
    while True:
        try:
            subprocess.run(bootstrap_cmd, check=True)
            return DeploymentResult.BOOTSTRAP_SUCCESS
        except subprocess.CalledProcessError as e:
            log.error(e)
            try_again = input('Try again (y/n)? ')
            if try_again == 'n':
                return DeploymentResult.BOOTSTRAP_FAILURE


def get_chef_nodes():
    cmd = ['knife', 'node', 'list']
    result = subprocess.run(cmd, capture_output=True, check=True, text=True)
    return result.stdout.splitlines()


def get_instance_tag(instance, tag_key):
    if instance.tags is None:
        return None
    for tag in instance.tags:
        if tag.get('Key') == tag_key:
            return tag.get('Value')


def get_keyfile(keyfile_name: str):
    if keyfile_name is None:
        return
    s = Settings()
    keyfile = s.keyfile_location / keyfile_name
    if keyfile.is_file():
        return keyfile


def get_ssh_user(instance, default: str = 'ec2-user') -> str:
    user_from_tag = get_instance_tag(instance, 'machine__ssh_user')
    if user_from_tag is None:
        return default
    return user_from_tag


def process_instance(region, instance) -> DeploymentResult:
    install_tag = get_instance_tag(instance, 'machine__install_chef')
    if install_tag == 'false':
        return DeploymentResult.EXCLUDED_WITH_TAG
    instance_state = instance.state.get('Name')
    if instance_state == 'pending':
        return DeploymentResult.INSTANCE_STATE_PENDING
    elif instance_state in ('running', 'stopped'):
        if instance.platform == 'windows':
            # return DeploymentResult.PLATFORM_IS_WINDOWS
            return process_instance_windows(region, instance)
        return process_instance_linux(region, instance)
    elif instance_state == 'shutting-down':
        return DeploymentResult.INSTANCE_STATE_SHUTTING_DOWN
    elif instance_state == 'terminated':
        return DeploymentResult.INSTANCE_STATE_TERMINATED
    elif instance_state == 'stopping':
        return DeploymentResult.INSTANCE_STATE_STOPPING


def process_instance_linux(region, instance) -> DeploymentResult:
    keyfile_name = get_instance_tag(instance, 'machine__ssh_keyfile')
    if keyfile_name is None:
        keyfile_name = instance.key_name
    if keyfile_name is None:
        log.error(f'aws.{region}.{instance.id} / unknown keyfile')
        return DeploymentResult.KEYFILE_UNKNOWN
    keyfile = get_keyfile(keyfile_name)
    if keyfile is None:
        log.error(f'aws.{region}.{instance.id} / missing keyfile {keyfile_name}')
        return DeploymentResult.KEYFILE_MISSING
    do_stop_instance = False
    if instance.state.get('Name') == 'stopped':
        do_stop_instance = True
        start_instance(region, instance)
    instance.load()
    hostname = instance.public_dns_name
    ssh_user = get_ssh_user(instance)
    result = DeploymentResult.BOOTSTRAP_FAILURE
    try:
        result = bootstrap_node_linux(hostname, ssh_user, f'aws.{region}.{instance.id}', keyfile)
    finally:
        if do_stop_instance:
            stop_instance(region, instance)
        return result


def process_instance_windows(region, instance) -> DeploymentResult:
    do_stop_instance = False
    if instance.state.get('Name') == 'stopped':
        do_stop_instance = True
        start_instance(region, instance)
        # return DeploymentResult.INSTANCE_STATE_STOPPED
    instance.load()
    hostname = instance.public_dns_name
    result = DeploymentResult.BOOTSTRAP_FAILURE
    try:
        result = bootstrap_node_windows(hostname, f'aws.{region}.{instance.id}')
    finally:
        if do_stop_instance:
            stop_instance(region, instance)
        return result


def start_instance(region, instance):
    log.info(f'Starting instance aws.{region}.{instance.id}')
    instance.start()
    instance.wait_until_running()
    log.info(f'Instance aws.{region}.{instance.id} is running')


def stop_instance(region, instance):
    log.info(f'Stopping instance aws.{region}.{instance.id}')
    instance.stop()
    instance.wait_until_stopped()
    log.info(f'Instance aws.{region}.{instance.id} is stopped')


def main_job():
    results = {}
    chef_nodes = get_chef_nodes()
    boto_session = boto3.session.Session()
    for region in boto_session.get_available_regions('ec2'):
        log.debug(f'checking {region}')
        ec2 = boto3.resource('ec2', region_name=region)
        try:
            for instance in ec2.instances.all():
                candidate_node_name = f'aws.{region}.{instance.id}'
                if candidate_node_name in chef_nodes:
                    result = DeploymentResult.CHEF_NODE_EXISTS
                else:
                    result = process_instance(region, instance)
                group = results.get(result, [])
                group.append(candidate_node_name)
                results.update({result: group})
        except botocore.exceptions.ClientError as e:
            log.critical(e)
    for result, group in results.items():
        log.info(f'### {result} ({len(group)})')
        if result.report_details():
            for item in group:
                log.info(f'  {item} {result.name}')


def main():
    s = Settings()
    logging.basicConfig(format=s.log_format, level=logging.DEBUG, stream=sys.stdout)
    log.debug(f'{log.name} {_version}')
    if not s.log_level == 'DEBUG':
        log.debug(f'Setting log level to {s.log_level}')
    logging.getLogger().setLevel(s.log_level)

    for logger, level in s.other_log_levels.items():
        log.debug(f'Setting log level for {logger} to {level}')
        logging.getLogger(logger).setLevel(level)

    if s.run_and_exit:
        main_job()
        return

    scheduler = apscheduler.schedulers.blocking.BlockingScheduler()
    scheduler.add_job(main_job, 'interval', minutes=s.run_interval)
    scheduler.add_job(main_job)
    scheduler.start()


def handle_sigterm(_signal, _frame):
    sys.exit()


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, handle_sigterm)
    main()
