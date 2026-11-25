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
def w(h,t=1000):
    with lock: lin.write(0x02,bytes.fromhex(h),t)
def r(t=100):
    try: return bytes(lin.read(0x81,4096,t))
    except usb.core.USBError: return None
print("init...",flush=True)
for dl,hx in OPS:
    if dl: time.sleep(dl/1000)
    w(hx); r(100)
print("armed",flush=True)
stop=False
def poller():
    while not stop:
        try:
            w("40031d",300); r(10); w("60031d",300); r(10)
        except usb.core.USBError:
            return
        time.sleep(0.01)
threading.Thread(target=poller,daemon=True).start()
time.sleep(2)
for i in range(4):
    try:
        w("60031e"); a1=r(150)
        print("1e before ring:", a1.hex() if a1 else "none", flush=True)
        w("6e0e0002500400001e0100015004")
        time.sleep(1.0)
        w("6e0e0002500400001e0200015004")
        time.sleep(2.0)
        print("cycle",i+1,flush=True)
    except usb.core.USBError as e:
        print("device died:", e); break
stop=True
print("done")
