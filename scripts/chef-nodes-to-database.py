import logging
import os
import psycopg2
import psycopg2.extras
import subprocess
import sys

LOG_FORMAT = os.getenv('LOG_FORMAT', '%(levelname)s [%(name)s] %(message)s')
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
logging.basicConfig(format=LOG_FORMAT, level=LOG_LEVEL, stream=sys.stdout)
log = logging.getLogger('chef-nodes-to-database')

cnx = psycopg2.connect(os.getenv('DB'))

result = subprocess.run(['knife', 'node', 'list'], capture_output=True, check=True, text=True)
node_names = [{'node_name': node_name} for node_name in result.stdout.splitlines()]
log.info(f'Found {len(node_names)} nodes')

with cnx:
    with cnx.cursor() as cur:
        cur.execute('update chef_nodes set synced = false where synced is true')
        sql = '''
            insert into chef_nodes (node_name, synced) values (%(node_name)s, true)
            on conflict (node_name) do update set synced = true
        '''
        psycopg2.extras.execute_batch(cur, sql, node_names)
        cur.execute('delete from chef_nodes where synced is false')

cnx.close()
