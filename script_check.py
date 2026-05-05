
import sqlite3, json
conn = sqlite3.connect('db.sqlite3')
cur = conn.cursor()
cur.execute("SELECT outputs FROM workflows_nodeexecution WHERE node_type='script.Task' ORDER BY id DESC LIMIT 1;")
row = cur.fetchone()
if row:
    outputs = json.loads(row[0])
    print('--- ERREUR COMPLETE ---')
    print(outputs.get('stderr', ''))
else:
    print('Aucun run')

