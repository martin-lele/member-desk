"""First-run local launcher (macOS/Linux; Python standard library)."""
import app,subprocess,sys,time,urllib.request,json,webbrowser
from pathlib import Path
if __name__=='__main__':
 app.init()
 port=8765
 url=f'http://127.0.0.1:{port}/'
 process=subprocess.Popen([sys.executable,str(app.ROOT/'app.py'),'--port',str(port)])
 try:
  for _ in range(40):
   if process.poll() is not None:raise RuntimeError('服务未启动，端口可能被其他程序占用')
   try:
    with urllib.request.urlopen(url+'api/bootstrap',timeout=1) as response:
     if json.load(response).get('application')=='local-member-desk':break
   except OSError:pass
   time.sleep(.25)
  else:raise RuntimeError('服务启动超时')
  token=app.DATA/'setup-token.txt'
  if token.exists():url+='#'+token.read_text().strip()
  print('浏览器将打开会员台；首次设置密钥只保存在本机 data/setup-token.txt。')
  webbrowser.open(url)
  process.wait()
 except KeyboardInterrupt:pass
 finally:
  if process.poll() is None:
   (app.DATA/f'stop-{port}').touch()
   process.wait(timeout=40)
