import boto3
import botocore.exceptions
import logging
import os
import subprocess

from . import profiles

LOG_FORMAT = os.getenv('LOG_FORMAT', '%(levelname)s [%(name)s] %(message)s')
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
logging.basicConfig(format=LOG_FORMAT, level=LOG_LEVEL)
log = logging.getLogger('prune-chef-nodes')

all_aws_instances = set()

for account_number, account_description in profiles.PROFILES.items():
    log.info(f'Working on profile {account_description} ({account_number})')
    session = boto3.session.Session(profile_name=account_description)
    for region in session.get_available_regions('ec2'):
        ec2 = session.resource('ec2', region_name=region)
        try:
            for instance in ec2.instances.all():
                all_aws_instances.add(f'aws.{region}.{instance.instance_id}')
            pass
        except botocore.exceptions.ClientError:
            log.warning(f'Skipping region {region}')

log.info(f'Found {len(all_aws_instances)} total instances in AWS')

result = subprocess.run(['knife', 'node', 'list'], capture_output=True, check=True, text=True)
all_chef_nodes = set(result.stdout.splitlines())

log.info(f'Found {len(all_chef_nodes)} nodes in Chef')

to_remove_from_chef = all_chef_nodes - all_aws_instances
log.info(f'Will remove {len(to_remove_from_chef)} nodes from Chef')

for node_name in to_remove_from_chef:
    for resource in ['node', 'client']:
        cmd = ['knife', resource, 'delete', node_name, '--yes']
        subprocess.run(cmd, check=True)
