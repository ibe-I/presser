#!/usr/bin/env python3

import time
import logging
from gpiozero import DigitalOutputDevice

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class CompressionRig:
    # GPIO pin configuration
    EN_PIN = 4
    MS1_PIN = 27
    MS2_PIN = 22
    STEP_PIN = 23
    DIR_PIN = 24
    
    # Direction constants
    DIR_UP = 1
    DIR_DOWN = 0
    
    # Motor parameters
    STEPS_PER_MM = 400  # Adjust based on your screw
    motor_enabled = False
    
    def __init__(self):
        """Initialize GPIO pins exactly like the printer does"""
        # Initialize the GPIO devices using gpiozero
        self.en_pin = DigitalOutputDevice(self.EN_PIN)
        self.ms1_pin = DigitalOutputDevice(self.MS1_PIN)
        self.ms2_pin = DigitalOutputDevice(self.MS2_PIN)
        self.step_pin = DigitalOutputDevice(self.STEP_PIN)
        self.dir_pin = DigitalOutputDevice(self.DIR_PIN)
        
        # Set up microstepping mode (full step mode)
        self.ms1_pin.off()
        self.ms2_pin.off()
        
        # Enable motor
        self.enable_motor()
        
        print("=" * 50)
        print("COMPRESSION TEST RIG")
        print("=" * 50)
        print()
    
    def enable_motor(self):
        """Enable the stepper motor (LOW enables)"""
        self.en_pin.off()  # LOW enables
        self.motor_enabled = True
        logger.info("Motor enabled")
        time.sleep(0.5)
    
    def press_down(self, steps=600):
        """Press down for specified steps"""
        if not self.motor_enabled:
            logger.warning("Motor not enabled!")
            return
        
        # Set direction DOWN
        self.dir_pin.value = self.DIR_DOWN
        time.sleep(0.1)
        
        logger.info("Pressing down " + str(steps) + " steps...")
        
        for i in range(steps):
            # Step pulse (exactly like printer)
            self.step_pin.on()
            time.sleep(0.0005)
            self.step_pin.off()
            time.sleep(0.0005)
            
            if (i + 1) % 100 == 0:
                logger.info("Position: " + str(i + 1))
        
        logger.info("COMPRESSION COMPLETE\n")
    
    def retract(self, steps=600):
        """Retract to home"""
        if not self.motor_enabled:
            logger.warning("Motor not enabled!")
            return
        
        # Set direction UP
        self.dir_pin.value = self.DIR_UP
        time.sleep(0.1)
        
        logger.info("Retracting " + str(steps) + " steps...")
        
        for i in range(steps):
            # Step pulse
            self.step_pin.on()
            time.sleep(0.0005)
            self.step_pin.off()
            time.sleep(0.0005)
            
            if (i + 1) % 100 == 0:
                logger.info("Retracted: " + str(i + 1))
        
        logger.info("FULLY RETRACTED\n")
    
    def cleanup(self):
        """Clean up GPIO"""
        self.en_pin.close()
        self.ms1_pin.close()
        self.ms2_pin.close()
        self.step_pin.close()
        self.dir_pin.close()
        logger.info("GPIO cleaned up")

# Main loop
if __name__ == "__main__":
    rig = CompressionRig()
    
    print("Commands: p=press, r=retract, q=quit\n")
    
    try:
        while True:
            cmd = input("compression> ").strip().lower()
            
            if cmd == 'p':
                rig.press_down(600)
            elif cmd.startswith('press '):
                steps = int(cmd.split()[1])
                rig.press_down(steps)
            elif cmd == 'r':
                rig.retract(600)
            elif cmd.startswith('retract '):
                steps = int(cmd.split()[1])
                rig.retract(steps)
            elif cmd == 'q':
                print("Goodbye!\n")
                break
            else:
                print("Unknown command")
    
    finally:
        rig.cleanup()
