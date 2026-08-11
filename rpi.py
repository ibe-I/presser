#!/usr/bin/env python3

import RPi.GPIO as GPIO
import time
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class CompressionTestRig:
    """Compression test rig - RPi.GPIO version for Pi5"""
    
    # GPIO Pin Configuration
    EN_PIN = 4
    MS1_PIN = 27
    MS2_PIN = 22
    STEP_PIN = 23
    DIR_PIN = 24
    
    # Direction constants
    DIR_UP = 1
    DIR_DOWN = 0
    
    # Motor settings
    STEP_DELAY = 0.0005
    
    def __init__(self):
        """Initialize GPIO using RPi.GPIO"""
        logger.info("Initializing Compression Test Rig on Pi5...")
        
        try:
            # Setup GPIO mode
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            
            # Setup all pins as outputs
            GPIO.setup(self.EN_PIN, GPIO.OUT)
            GPIO.setup(self.MS1_PIN, GPIO.OUT)
            GPIO.setup(self.MS2_PIN, GPIO.OUT)
            GPIO.setup(self.STEP_PIN, GPIO.OUT)
            GPIO.setup(self.DIR_PIN, GPIO.OUT)
            
            logger.info("GPIO pins configured")
            
            # Set initial states
            GPIO.output(self.MS1_PIN, GPIO.LOW)   # Full step mode
            GPIO.output(self.MS2_PIN, GPIO.LOW)
            GPIO.output(self.STEP_PIN, GPIO.LOW)
            GPIO.output(self.DIR_PIN, GPIO.LOW)
            
            # Enable motor (LOW enables)
            GPIO.output(self.EN_PIN, GPIO.LOW)
            time.sleep(0.5)
            logger.info("Motor ENABLED")
            
            print("=" * 60)
            print("COMPRESSION TEST RIG - Raspberry Pi 5")
            print("Using RPi.GPIO for Pi5 compatibility")
            print("=" * 60)
            print()
            
        except Exception as e:
            logger.error(f"Initialization failed: {e}")
            GPIO.cleanup()
            raise
    
    def press_down(self, steps=600):
        """Press down for specified steps"""
        try:
            # Set direction DOWN
            GPIO.output(self.DIR_PIN, self.DIR_DOWN)
            time.sleep(0.1)
            
            logger.info(f"Pressing down {steps} steps")
            print(f"\n>>> PRESSING DOWN {steps} STEPS <<<\n")
            
            # Send step pulses
            for i in range(steps):
                GPIO.output(self.STEP_PIN, GPIO.HIGH)
                time.sleep(self.STEP_DELAY / 2)
                GPIO.output(self.STEP_PIN, GPIO.LOW)
                time.sleep(self.STEP_DELAY / 2)
                
                if (i + 1) % 100 == 0:
                    print(f"Position: {i + 1} / {steps}")
            
            print("\nCOMPRESSION COMPLETE - Read multimeter\n")
            logger.info("Compression complete")
            
        except Exception as e:
            logger.error(f"Error during press_down: {e}")
    
    def retract(self, steps=600):
        """Retract to home"""
        try:
            # Set direction UP
            GPIO.output(self.DIR_PIN, self.DIR_UP)
            time.sleep(0.1)
            
            logger.info(f"Retracting {steps} steps")
            print(f"\n>>> RETRACTING {steps} STEPS <<<\n")
            
            # Send step pulses
            for i in range(steps):
                GPIO.output(self.STEP_PIN, GPIO.HIGH)
                time.sleep(self.STEP_DELAY / 2)
                GPIO.output(self.STEP_PIN, GPIO.LOW)
                time.sleep(self.STEP_DELAY / 2)
                
                if (i + 1) % 100 == 0:
                    print(f"Retracted: {i + 1} / {steps}")
            
            print("\nFULLY RETRACTED\n")
            logger.info("Retraction complete")
            
        except Exception as e:
            logger.error(f"Error during retract: {e}")
    
    def cleanup(self):
        """Clean up GPIO"""
        try:
            GPIO.output(self.EN_PIN, GPIO.HIGH)  # Disable motor
            time.sleep(0.1)
            GPIO.cleanup()
            logger.info("GPIO cleaned up")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")


# Main program
if __name__ == "__main__":
    rig = None
    
    try:
        rig = CompressionTestRig()
        
        print("Commands:")
        print("  p              - Press down (600 steps)")
        print("  press 800      - Press down 800 steps")
        print("  r              - Retract (600 steps)")
        print("  retract 800    - Retract 800 steps")
        print("  q              - Quit\n")
        
        while True:
            try:
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
                    if cmd:
                        print("Unknown command")
            
            except KeyboardInterrupt:
                print("\n\nInterrupted")
                break
    
    except Exception as e:
        logger.error(f"Fatal error: {e}")
    
    finally:
        if rig:
            rig.cleanup()
