sudo python3 << 'EOF'
from gpiozero import LED
import time

EN = LED(4, active_high=False)    # Enable motor
DIR = LED(24, active_high=False)  # Direction
STP = LED(23, active_high=False)  # Step

print("Enabling motor on GPIO 4...")
EN.on()
time.sleep(0.5)

print("Setting direction...")
DIR.off()
time.sleep(0.1)

print("Sending 200 step pulses...")
for i in range(200):
    STP.on()
    time.sleep(0.0005)
    STP.off()
    time.sleep(0.0005)
    if (i + 1) % 50 == 0:
        print("Steps: " + str(i + 1))

print("Done")
EN.close()
DIR.close()
STP.close()
EOF
