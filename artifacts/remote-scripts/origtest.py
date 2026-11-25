import sys, os, time, glob, termios
sys.path.insert(0,'/tmp/linbridge')
sys.path.insert(0,'/tmp/linbridge')
import usb.core, usb.util
import importlib.util
spec=importlib.util.spec_from_file_location("isq","/tmp/linbridge/initsequence.py")
isq=importlib.util.module_from_spec(spec); spec.loader.exec_module(isq)
OPS=isq.OPS
VID=0x31b7
d=usb.core.find(idVendor=VID)
if d and d.idProduct!=2:
    try: d.reset()
    except Exception: pass
    time.sleep(3)
d=usb.core.find(idVendor=VID)
if d and d.idProduct==2:
    tty=sorted(glob.glob('/dev/ttyACM*'))[0]
    fd=os.open(tty,os.O_RDWR|os.O_NOCTTY|os.O_NONBLOCK)
    a=termios.tcgetattr(fd);a[3]=0;termios.tcsetattr(fd,termios.TCSANOW,a)
    os.write(fd,b'b');os.close(fd);time.sleep(3.5)
lin=usb.core.find(idVendor=VID,idProduct=1)
if not lin: print("no dev"); sys.exit(1)
try: lin.set_configuration()
except Exception: pass
try: usb.util.claim_interface(lin, lin.get_active_configuration()[(0,0)])
except Exception: pass
def w(h,t=1500): lin.write(0x02,bytes.fromhex(h),t)
def r(t=60):
    try: return bytes(lin.read(0x81,4096,t))
    except usb.core.USBError: return None
print("ops:",len(OPS),flush=True)
for i,(dl,hx) in enumerate(OPS):
    if dl: time.sleep(dl/1000.0)
    try:
        w(hx)
    except usb.core.USBError as e:
        print("DIE at op",i,hx[:24],e,flush=True); sys.exit(1)
    rep=r(120)
    if hx[:6] in ("40031d","60031d") and rep and len(rep)>=3 and rep[2]!=0:
        tries=0
        while tries<40:
            w(hx); rep=r(120); tries+=1
            if not rep or (len(rep)>=3 and rep[2]==0): break
    if i%60==0: print("op",i,flush=True)
print("init complete",flush=True)
for addr in ["1e","0b","1d"]:
    w(f"6003{addr}"); a=r(200); w(f"4003{addr}"); b=r(200)
    print(f"reg {addr}: p1={a.hex() if a else None} p2={b.hex() if b else None}",flush=True)
