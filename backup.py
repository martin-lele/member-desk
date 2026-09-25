"""User-initiated consistent local snapshot; backups are not changed by member deletion."""
import datetime,sqlite3
from pathlib import Path
from app import ROOT,connect,init
init();folder=ROOT/'backups';folder.mkdir(exist_ok=True);folder.chmod(0o700)
file=folder/('members-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.sqlite3')
source=connect();target=sqlite3.connect(file)
try:source.backup(target)
finally:target.close();source.close()
file.chmod(0o600);print('备份已保存：'+str(file))
