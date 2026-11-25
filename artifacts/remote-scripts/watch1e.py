import sys, os, time, glob, termios, threading
sys.path.insert(0,'/tmp/linbridge')
import usb.core, usb.util
from capture_init import OPS
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
try: lin.set_configuration()
except Exception: pass
try: usb.util.claim_interface(lin, lin.get_active_configuration()[(0,0)])
except Exception: pass
lock=threading.Lock()
def w(h,t=800):
    with lock: lin.write(0x02,bytes.fromhex(h),t)
def r(t=80):
    try: return bytes(lin.read(0x81,4096,t))
    except usb.core.USBError: return None
for dl,hx in OPS:
    if dl: time.sleep(dl/1000)
    w(hx); r(80)
print("armed", flush=True)
for i in range(10):
    w("60031e"); a=r(120)
    w("40031e"); b=r(120)
    try:
        w("60031d"); c=r(60); w("40031d"); d2=r(60)
    except usb.core.USBError as e:
        print("died", e); break
    print(i, "1e_p1=", a.hex() if a else None, "1e_p2=", b.hex() if b else None,
          "1d_p1=", c.hex() if c else None, "1d_p2=", d2.hex() if d2 else None, flush=True)
    time.sleep(1)
