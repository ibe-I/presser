#!/usr/bin/env python3

import os
os.environ['GPIOZERO_PIN_FACTORY'] = 'native'

from gpiozero import DigitalOutputDevice
import time
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class CompressionTestRig:
    """Compression test rig - using printer's exact gpiozero method"""
    
    EN_PIN = 4
    MS1_PIN = 27
    MS2_PIN = 22
    STEP_PIN = 23
    DIR_PIN = 24
    
    DIR_UP = 1
    DIR_DOWN = 0
    
    def __init__(self):
        logger.info("Initializing Compression Test Rig (printer method)...")
        
        try:
            # Initialize GPIO devices EXACTLY like the printer does
            self.en_pin = DigitalOutputDevice(self.EN_PIN)
            self.ms1_pin = DigitalOutputDevice(self.MS1_PIN)
            self.ms2_pin = DigitalOutputDevice(self.MS2_PIN)
            self.step_pin = DigitalOutputDevice(self.STEP_PIN)
            self.dir_pin = DigitalOutputDevice(self.DIR_PIN)
            
            logger.info("GPIO devices initialized")
            
            # Set up microstepping mode (full step mode)
            self.ms1_pin.off()
            self.ms2_pin.off()
            
            # Set initial states
            self.step_pin.off()
            self.dir_pin.off()
            time.sleep(0.1)
            
            # Enable motor (LOW = enable)
            self.en_pin.off()
            time.sleep(0.5)
            logger.info("Motor ENABLED")
            
            print("=" * 60)
            print("COMPRESSION TEST RIG - Pi5")
            print("Using printer's gpiozero method")
            print("=" * 60)
            print()
            
        except Exception as e:
            logger.error(f"Initialization failed: {e}")
            raise
    
    def press_down(self, steps=600):
        """Press down - like printer does"""
        try:
            # Set direction DOWN (printer method)
            self.dir_pin.value = self.DIR_DOWN
            time.sleep(0.1)
            
            logger.info(f"Pressing down {steps} steps")
            print(f"\n>>> PRESSING DOWN {steps} STEPS <<<\n")
            
            # Send step pulses (exact printer pattern)
            for i in range(steps):
                self.step_pin.on()
                time.sleep(0.00025)
                self.step_pin.off()
                time.sleep(0.00025)
                
                if (i + 1) % 100 == 0:
                    print(f"Position: {i + 1}")
            
            print("\nCOMPRESSION COMPLETE\n")
            logger.info("Compression complete")
        
        except Exception as e:
            logger.error(f"Error: {e}")
    
    def retract(self, steps=600):
        """Retract - like printer does"""
        try:
            # Set direction UP (printer method)
            self.dir_pin.value = self.DIR_UP
            time.sleep(0.1)
            
            logger.info(f"Retracting {steps} steps")
            print(f"\n>>> RETRACTING {steps} STEPS <<<\n")
            
            # Send step pulses
            for i in range(steps):
                self.step_pin.on()
                time.sleep(0.00025)
                self.step_pin.off()
                time.sleep(0.00025)
                
                if (i + 1) % 100 == 0:
                    print(f"Retracted: {i + 1}")
            
            print("\nFULLY RETRACTED\n")
            logger.info("Retraction complete")
        
        except Exception as e:
            logger.error(f"Error: {e}")
    
    def cleanup(self):
        """Cleanup like printer"""
        try:
            self.en_pin.on()  # Disable
            self.en_pin.close()
            self.step_pin.close()
            self.dir_pin.close()
            self.ms1_pin.close()
            self.ms2_pin.close()
            logger.info("GPIO cleaned up")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")


if __name__ == "__main__":
    rig = None
    try:
        rig = CompressionTestRig()
        print("Commands: p, press 800, r, retract 800, q\n")
        
        while True:
            cmd = input("compression> ").strip().lower()
            if cmd == 'p':
                rig.press_down(600)
            elif cmd.startswith('press '):
                rig.press_down(int(cmd.split()[1]))
            elif cmd == 'r':
                rig.retract(600)
            elif cmd.startswith('retract '):
                rig.retract(int(cmd.split()[1]))
            elif cmd == 'q':
                break
            else:
                if cmd:
                    print("Unknown command")
    
    except KeyboardInterrupt:
        print("\n\nInterrupted")
    
    finally:
        if rig:
            rig.cleanup()
