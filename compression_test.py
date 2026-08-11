#!/usr/bin/env python3

import time
import logging
from gpiozero import DigitalOutputDevice

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class CompressionTestRig:
    """Compression test rig - based on MATIC printer motor control"""
    
    # GPIO Pin Configuration (from your motherboard)
    EN_PIN = 4
    MS1_PIN = 27
    MS2_PIN = 22
    STEP_PIN = 23
    DIR_PIN = 24
    
    # Direction constants
    DIR_UP = 1
    DIR_DOWN = 0
    
    # Motor settings
    STEP_DELAY = 0.0005  # 500 microseconds (matching printer)
    ACCELERATION_ENABLED = False
    
    def __init__(self):
        """Initialize GPIO pins exactly like the printer does"""
        logger.info("Initializing Compression Test Rig...")
        
        try:
            # Initialize the GPIO devices using gpiozero (same as printer)
            self.en_pin = DigitalOutputDevice(self.EN_PIN)
            self.ms1_pin = DigitalOutputDevice(self.MS1_PIN)
            self.ms2_pin = DigitalOutputDevice(self.MS2_PIN)
            self.step_pin = DigitalOutputDevice(self.STEP_PIN)
            self.dir_pin = DigitalOutputDevice(self.DIR_PIN)
            
            logger.info("GPIO devices initialized")
            
            # Set up microstepping mode (full step mode - same as printer)
            self.ms1_pin.off()  # MS1 = LOW
            self.ms2_pin.off()  # MS2 = LOW
            logger.info("Microstepping set to full step mode")
            
            # Critical: Set initial state of step and dir pins LOW
            self.step_pin.off()
            self.dir_pin.off()
            time.sleep(0.1)
            
            # Enable motor (LOW enables, same as printer)
            self.en_pin.off()
            time.sleep(0.5)
            logger.info("Motor ENABLED")
            
            print("=" * 60)
            print("COMPRESSION TEST RIG - Raspberry Pi 5")
            print("Motor: NEMA 23 Stepper")
            print("Control: GPIO 23(STEP), GPIO 24(DIR), GPIO 4(EN)")
            print("=" * 60)
            print()
            
        except Exception as e:
            logger.error(f"Initialization failed: {e}")
            raise
    
    def press_down(self, steps=600, speed_delay=None):
        """Press down for specified steps (compress polymer)"""
        if speed_delay is None:
            speed_delay = self.STEP_DELAY
        
        try:
            # Set direction DOWN (same as printer)
            self.dir_pin.value = self.DIR_DOWN
            time.sleep(0.1)
            
            logger.info(f"Pressing down {steps} steps at speed {1/speed_delay:.0f} Hz")
            print(f"\n>>> PRESSING DOWN {steps} STEPS <<<\n")
            
            # Send step pulses (exact same pattern as printer)
            for i in range(steps):
                self.step_pin.on()
                time.sleep(speed_delay / 2)
                self.step_pin.off()
                time.sleep(speed_delay / 2)
                
                # Progress update
                if (i + 1) % 100 == 0:
                    print(f"Position: {i + 1} / {steps}")
            
            print("\nCOMPRESSION COMPLETE - Read multimeter now\n")
            logger.info(f"Compression complete at position {steps}")
            
        except Exception as e:
            logger.error(f"Error during press_down: {e}")
    
    def retract(self, steps=600, speed_delay=None):
        """Retract to home (release polymer)"""
        if speed_delay is None:
            speed_delay = self.STEP_DELAY
        
        try:
            # Set direction UP (same as printer)
            self.dir_pin.value = self.DIR_UP
            time.sleep(0.1)
            
            logger.info(f"Retracting {steps} steps")
            print(f"\n>>> RETRACTING {steps} STEPS <<<\n")
            
            # Send step pulses
            for i in range(steps):
                self.step_pin.on()
                time.sleep(speed_delay / 2)
                self.step_pin.off()
                time.sleep(speed_delay / 2)
                
                if (i + 1) % 100 == 0:
                    print(f"Retracted: {i + 1} / {steps}")
            
            print("\nFULLY RETRACTED\n")
            logger.info("Retraction complete")
            
        except Exception as e:
            logger.error(f"Error during retract: {e}")
    
    def cleanup(self):
        """Clean up GPIO - safe shutdown"""
        try:
            # Disable motor before cleanup
            self.en_pin.on()  # HIGH disables
            time.sleep(0.1)
            
            # Close all pins
            self.en_pin.close()
            self.ms1_pin.close()
            self.ms2_pin.close()
            self.step_pin.close()
            self.dir_pin.close()
            
            logger.info("GPIO cleaned up successfully")
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
        print("  r              - Retract to home (600 steps)")
        print("  retract 800    - Retract 800 steps")
        print("  speed 1000     - Set speed (microseconds per step)")
        print("  q              - Quit\n")
        
        current_speed = 0.0005  # Default 500 microseconds
        
        while True:
            try:
                cmd = input("compression> ").strip().lower()
                
                if cmd == 'p':
                    rig.press_down(600, current_speed)
                
                elif cmd.startswith('press '):
                    try:
                        steps = int(cmd.split()[1])
                        rig.press_down(steps, current_speed)
                    except (IndexError, ValueError):
                        print("Usage: press 800")
                
                elif cmd == 'r':
                    rig.retract(600, current_speed)
                
                elif cmd.startswith('retract '):
                    try:
                        steps = int(cmd.split()[1])
                        rig.retract(steps, current_speed)
                    except (IndexError, ValueError):
                        print("Usage: retract 800")
                
                elif cmd.startswith('speed '):
                    try:
                        microseconds = int(cmd.split()[1])
                        current_speed = microseconds / 1_000_000
                        print(f"Speed set to {microseconds} microseconds/step")
                    except (IndexError, ValueError):
                        print("Usage: speed 1000 (microseconds)")
                
                elif cmd == 'q':
                    print("Goodbye!\n")
                    break
                
                elif cmd == 'h' or cmd == 'help':
                    print("Commands: p, press N, r, retract N, speed N, q")
                
                else:
                    if cmd:
                        print(f"Unknown command: {cmd}")
            
            except KeyboardInterrupt:
                print("\n\nInterrupted by user")
                break
    
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        print(f"Error: {e}")
    
    finally:
        if rig:
            rig.cleanup()
        print("Program terminated")
