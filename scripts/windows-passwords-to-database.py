import configparser
import os
import psycopg2.extras

passwords_config = configparser.ConfigParser(interpolation=None)
passwords_file = os.getenv('PASSWORDS_FILE')
passwords_config.read(passwords_file)
passwords = passwords_config['passwords']

records = []
for name, password in passwords.items():
    records.append({
        'cloud': 'aws',
        'machine_id': name[14:],
        'password': password
    })

cnx = psycopg2.connect(os.getenv('DB'))
with cnx:
    with cnx.cursor() as cur:
        sql = '''
            insert into windows_credentials (cloud, machine_id, password)
            values (%(cloud)s, %(machine_id)s, %(password)s)
            on conflict (machine_id) do update set password = %(password)s
        '''
        psycopg2.extras.execute_batch(cur, sql, records)

cnx.close()
