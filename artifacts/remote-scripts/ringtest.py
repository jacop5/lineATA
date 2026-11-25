import sys, time
sys.path.insert(0,'/tmp/linbridge')
import usb.core, usb.util
from capture_init import OPS
VID=0x31b7
# cold boot
d=usb.core.find(idVendor=VID)
import os,termios,glob
if d and d.idProduct!=2:
    try: d.reset()
    except Exception: pass
    time.sleep(3)
d=usb.core.find(idVendor=VID)
if d and d.idProduct==2:
    tty=sorted(glob.glob('/dev/ttyACM*'))[0]
    fd=os.open(tty,os.O_RDWR|os.O_NOCTTY|os.O_NONBLOCK)
    a=termios.tcgetattr(fd);a[3]=0;termios.tcsetattr(fd,termios.TCSANOW,a)
    os.write(fd,b'b');os.close(fd)
    time.sleep(3.5)
lin=usb.core.find(idVendor=VID,idProduct=1)
if not lin: print("no dev"); sys.exit(1)
try: lin.set_configuration()
except Exception: pass
try: usb.util.claim_interface(lin, lin.get_active_configuration()[(0,0)])
except Exception: pass
def w(h,t=1000): lin.write(0x02,bytes.fromhex(h),t)
def r(t=100):
    try: return bytes(lin.read(0x81,4096,t))
    except usb.core.USBError: return None
print("init...",flush=True)
for dl,hx in OPS:
    if dl: time.sleep(dl/1000)
    w(hx); r(100)
print("armed; polling 2s",flush=True)
t0=time.time()
while time.time()-t0 < 2.0:
    w("40031d"); r(15); w("60031d"); r(15)
rep=r(50); print("pre-ring 1e:", rep.hex() if rep else "none", flush=True)
w("60031e"); rep=r(120); print("read1e:", rep.hex() if rep else "none", flush=True)
print("ringing 3 times",flush=True)
for i in range(3):
    w("60031e"); rep=r(120); print("read1e:", rep.hex() if rep else "none",flush=True)
    w("6e0e0002500400001e0100015004")
    w("40031e")
    time.sleep(1.0)
    w("60031e")
    w("6e0e0002500400001e0200015004")
    w("40031e")
    time.sleep(2.0)
    print("ring cycle",i+1,flush=True)
print("done")
