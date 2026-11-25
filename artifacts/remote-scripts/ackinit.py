import sys, os, time, glob, termios
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
def w(h,t=1500): lin.write(0x02,bytes.fromhex(h),t)
def r(t=250):
    try: return bytes(lin.read(0x81,4096,t))
    except usb.core.USBError: return None
events=[]
bad=0
for i,(dl,hx) in enumerate(OPS):
    if dl: time.sleep(dl/1000)
    want=bytes.fromhex(hx)[0]
    ok=False
    for attempt in range(3):
        try: w(hx)
        except usb.core.USBError as e:
            print("DIE at op",i,hx[:20],e); sys.exit(1)
        # collect replies until we see our ack or timeout
        got=None
        t0=time.time()
        while time.time()-t0 < 0.25:
            rep=r(80)
            if not rep: continue
            if rep[0] in (0xa4,0xa5,0x64) and len(rep)<10:
                events.append(rep); continue
            got=rep; break
        if got is not None and got[0]==want:
            ok=True; break
    if not ok:
        bad+=1
        print("NOACK op",i,hx[:24], "got", got.hex() if got else None, flush=True)
print("init done, bad acks:", bad, "events:", len(events), flush=True)
for nm,addr in [("1e","1e"),("0b","0b")]:
    w(f"6003{addr}"); a=r(200); w(f"4003{addr}"); b=r(200)
    print(f"reg {addr}: p1={a.hex() if a else None} p2={b.hex() if b else None}", flush=True)
