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
lk=threading.Lock()
def w(h,t=800):
    with lk: lin.write(0x02,bytes.fromhex(h),t)
def r(t=60):
    try: return bytes(lin.read(0x81,4096,t))
    except usb.core.USBError: return None
for dl,hx in OPS:
    if dl: time.sleep(dl/1000)
    w(hx); r(60)
print("armed; polling 12s",flush=True)
stop=False
def poller():
    while not stop:
        try:
            w("40031d",300); r(8); w("60031d",300); r(8)
        except usb.core.USBError: return
        time.sleep(0.01)
threading.Thread(target=poller,daemon=True).start()
for i in range(12):
    time.sleep(1)
    try:
        w("60031e"); a=r(150); w("40031e"); b=r(150)
        print(i+1,"s: p1=",a.hex() if a else None,"p2=",b.hex() if b else None,flush=True)
    except usb.core.USBError as e:
        print("died",e); break
stop=True
