import os
import time
import glob
import signal
import subprocess
import threading
import re
import zipfile
import shutil
import json
import configparser
import tempfile 
from datetime import datetime, timedelta
try:
    from gpiozero import DigitalOutputDevice, DigitalInputDevice
except Exception as _gpio_imp_err:
    # Integration box without the GPIO stack (MATIC_ALLOW_NO_SCREEN=1): the
    # firmware imports and serves the web UI, hardware devices just refuse to
    # construct (setup_gpio's failure is caught downstream). A real printer
    # has gpiozero and hits the normal import above.
    if os.environ.get('MATIC_ALLOW_NO_SCREEN') != '1':
        raise
    class _NoGPIO:
        def __init__(self, *a, **k):
            raise RuntimeError('GPIO stack not installed (integration mode)')
    DigitalOutputDevice = DigitalInputDevice = _NoGPIO
from ip_service import start_discovery_service, stop_discovery_service
import numpy as np
from PIL import Image
from flask import Flask, render_template, request, jsonify, send_from_directory, send_file
import socket
import logging
from werkzeug.utils import secure_filename
import socket
import secrets
import requests

# Import the unified display driver
from Display import create_display
import health
import core_mode
import matic_paths

# Import the enhanced auto-slicing functionality
try:
    from MATIC_Slicer import AutoSlicer
except ImportError as e:
    print(f"Failed to import AutoSlicer: {e}")
    print("Make sure MATIC_Slicer.py is in the same directory as this file")
    raise

# Import the HTML template loader
try:
    from template_loader import load_html_template, load_find_printer_template
except ImportError as e:
    print(f"Failed to import template_loader: {e}")
    print("Make sure template_loader.py is in the same directory as this file")
    raise

# Import MATIC Cure module
try:
    from Cure import MaticCure
except ImportError as e:
    print(f"Failed to import MaticCure: {e}")
    print("Make sure Cure.py is in the same directory as this file")
    MaticCure = None

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Suppress Flask/werkzeug request logging to reduce clutter
logging.getLogger('werkzeug').setLevel(logging.WARNING)

# Configuration - Edit IP address here if needed
IP_ADDRESS = None
PORT = 5000
HOME_POSITION = -125.0  # Home position in mm (platform fully retracted)

# MATIC Smart Engine bridge — set by combined_server.py when the Smart Engine
# runs in this process (merged deploy). None = classic standalone firmware;
# every bridge-dependent route falls back to the legacy path in that case.
engine_bridge = None

FIRMWARE_VERSION = "MATIC Firmware Version: AEGIS"
FIRMWARE_UPDATE = None

# BUILD_NUMBER is a monotonic integer used by the cloud update system to
# decide whether a printer is older than the latest published build. It is
# read from version.txt at import time. MATIC_UPDATER.py increments it,
# MATIC_DEPLOY.py pins to the current dev value (deploys never bump).
# BUILD_DATE is the human-readable build date set by either deploy or update.
def _read_build_number():
    for path in (
        matic_paths.VERSION_TXT,
        '/home/MATIC/BETA/version.txt',
    ):
        try:
            if os.path.exists(path):
                with open(path) as f:
                    return int(f.read().strip())
        except Exception:
            pass
    return 0

BUILD_NUMBER = _read_build_number()
BUILD_DATE = "2026-06-23"

# ---------------------------------------------------------------------------
# PWA icon generator
# ---------------------------------------------------------------------------
# A Progressive Web App needs a manifest + icons. The Windows installer
# uses the 192px icon as the desktop / Start Menu shortcut icon for the
# Chrome --app launcher; Chrome / Edge also offer an in-browser "Install"
# button at /control on top of that. Generating icons procedurally keeps
# the firmware as a single .py-only build with no static assets to ship.
_PWA_ICON_CACHE = {}
_PWA_ICON_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logo.png')

def _generate_pwa_icon(size):
    """Return PNG bytes for an icon of the requested size (cached after first call).

    Sources logo.png next to main.py and resizes it. Falls back to a procedural
    red square with a white "M" if the file is missing or unreadable, so the
    PWA icon endpoints never 500."""
    if size in _PWA_ICON_CACHE:
        return _PWA_ICON_CACHE[size]
    import io
    from PIL import Image
    try:
        src = Image.open(_PWA_ICON_SOURCE).convert('RGBA')
        # LANCZOS keeps edges of a logo crisp at 192/512px far better than the
        # default BILINEAR. Pillow >= 10 renamed ANTIALIAS but LANCZOS is stable.
        img = src.resize((size, size), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format='PNG', optimize=True)
        data = buf.getvalue()
        _PWA_ICON_CACHE[size] = data
        return data
    except (OSError, ValueError) as e:
        logger.warning(f"PWA icon source missing, using procedural fallback: {e}")

    from PIL import ImageDraw
    try:
        from PIL import ImageFont
        font = None
        for path in (
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
            '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
        ):
            try:
                font = ImageFont.truetype(path, int(size * 0.62))
                break
            except OSError:
                continue
        if font is None:
            font = ImageFont.load_default()
    except Exception:
        font = None

    img = Image.new('RGB', (size, size), (192, 57, 43))
    draw = ImageDraw.Draw(img)
    if font is not None:
        bbox = draw.textbbox((0, 0), 'M', font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = (size - tw) / 2 - bbox[0]
        y = (size - th) / 2 - bbox[1]
        draw.text((x, y), 'M', fill=(255, 255, 255), font=font)
    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    # Do not cache the fallback: if logo.png appears later (e.g. dropped
    # in after the service started), the next request should pick it up.
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Design Portal config
# ---------------------------------------------------------------------------
# Pi-side state for the Design Portal feature. The Pi proxies upload + history
# requests to api.matic-3d.com; the cloud agent polls /api/design/pending and
# ingests results into Uploads/ via the internal /api/design/_ingest endpoint.
# The registry file maps every design-portal-origin filename in Uploads/
# (and its sliced descendants) to the originating job_id + patient_name so the
# UI can badge them. The ingest secret is a process-local token issued by
# main.py on first read; the cloud agent reads the same file to authorise its
# loopback POST. Never sent over the network.
DESIGN_API_BASE = 'https://api.matic-3d.com'
DESIGN_ORIGINS_FILE = matic_paths.DESIGN_ORIGINS
DESIGN_INGEST_SECRET_FILE = matic_paths.DESIGN_INGEST_SECRET
DESIGN_API_TIMEOUT_S = 10           # for heartbeat-cadence calls (history)
DESIGN_API_UPLOAD_TIMEOUT_S = 120   # scan upload may be slow on cell links

def ensure_cloud_agent():
    """Ensure cloud agent service exists and is running"""
    service_path = '/etc/systemd/system/matic-cloud-agent.service'
    agent_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cloud_agent.pyc')

    if not os.path.exists(agent_file) or os.path.exists(service_path):
        return

    try:
        working_dir = os.path.dirname(os.path.abspath(__file__))
        service = (
            "[Unit]\nDescription=MATIC Cloud Agent\n"
            "After=network-online.target\nWants=network-online.target\n\n"
            "[Service]\nType=simple\nUser=MATIC\nGroup=MATIC\n"
            f"WorkingDirectory={working_dir}\n"
            f"ExecStart=/usr/bin/python3 {agent_file}\n"
            "Restart=always\nRestartSec=30\n\n"
            "[Install]\nWantedBy=multi-user.target\n"
        )
        tmp_path = '/tmp/matic-cloud-agent.service'
        with open(tmp_path, 'w') as f:
            f.write(service)
        subprocess.run(['sudo', 'cp', tmp_path, service_path], check=True)
        subprocess.run(['sudo', 'systemctl', 'daemon-reload'], check=True)
        subprocess.run(['sudo', 'systemctl', 'enable', 'matic-cloud-agent'], check=True)
        subprocess.run(['sudo', 'systemctl', 'start', 'matic-cloud-agent'], check=True)
        logger.info("Cloud agent service created and started")
    except Exception as e:
        logger.error(f"Failed to create cloud agent service: {e}")

# Run at module level - executes on import, before any hardware init
ensure_cloud_agent()

class MaticLCDPrinter:
    def __init__(self):
        # Get IP address using hostname -I
        try:
            result = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                ips = result.stdout.strip().split()
                for ip in ips:
                    if ip and not ip.startswith('127.') and re.match(r'^(\d{1,3}\.){3}\d{1,3}$', ip):
                        self.ip_address = ip
                        logger.info(f"Auto-detected IP address: {self.ip_address}")
                        break
                else:
                    self.ip_address = "0.0.0.0"
                    logger.warning("No valid IP found - printer accessible on any network interface")
            else:
                self.ip_address = "0.0.0.0"
                logger.error("hostname -I command failed - using fallback")
        except Exception as e:
            logger.error(f"IP detection failed: {e}")
            self.ip_address = "0.0.0.0"
            logger.warning("Using fallback - printer accessible on any network interface")

        # Get hardware serial
        from device_id import DeviceID
        self.device_serial = DeviceID().get_device_id()
        logger.info(f"Hardware ID: {self.device_serial}")

        self.port = PORT
        # Initialize Enhanced AutoSlicer
        self.auto_slicer = AutoSlicer()
        
        # ============================================================================
        # SCALING COMPENSATION SETTINGS
        # ============================================================================
        # Horizontal scaling compensation factor - adjust as needed
        # Factor of 0.948 compensates for measured horizontal expansion
        self.horizontal_scale_factor = 1

        # Horizontal offset (in pixels) - negative values move image left
        # An offset of -40 centers the image properly on the display
        self.horizontal_offset = 0
        # ============================================================================

        # GPIO pins for stepper motor control
        self.EN_PIN = 4    # Enable pin
        self.MS1_PIN = 27   # Microstepping control 1
        self.MS2_PIN = 22   # Microstepping control 2
        self.STEP_PIN = 23  # Step pin
        self.DIR_PIN = 24   # Direction pin
        
        # UV Light control pin
        self.UV_LIGHT_PIN = 18  # GPIO pin for controlling UV light via IRFZ44N MOSFET
        
        # Proximity sensor pin
        self.PROXIMITY_SENSOR_PIN = 13  # GPIO pin for proximity sensor

        # Direction settings - REVERSED as requested
        self.DIR_UP = 1     # Direction for upward movement
        self.DIR_DOWN = 0   # Direction for downward movement
        
        # Step timing
        self.STEP_DELAY = 0.00001  # Delay between steps in seconds
        
        # Configuration
        self.STEPS_PER_MM = 400  # Adjust based on your specific setup
        self.MOTOR_SPEED = 3600
        self.move_home_speed = 3600  # Speed at whhich platform moves to home position
        self.white_ratio = 0  # Keep for compatibility with existing movement calculations
        self.current_layer_white_ratio = 0  # For UI display
        self.next_layer_white_ratio = 0     # For UI display
        self.ratio_multiplier = 8
        self.BOTTOM_LIFT_SPEED = 400     # Slow lifting for bottom layers             
        self.NORLAM_DECENT_SPEED = 3600  # Normal decent for regular layers
        self.NORMAL_LIFT_SPEED = 3600    # Normal lifting for regular layers
        self.normal_raising_height = 4
        self.bottom_raising_height = 8
        
        # Support tip detection settings
        self.SUPPORT_TIP_RATIO_THRESHOLD = 30   # NEW: Lowered threshold for detecting support tip transition (0=48 = 48 increase)
        self.SUPPORT_TIP_LIFT_SPEED = 400       # NEW: Slow speed for support tip transitions
        
        # Current position tracking
        self.current_position_mm = HOME_POSITION
        self.position_changed = False  # Flag to indicate when position has changed
        
        # Motor state
        self.motor_enabled = True
        
        # Path settings - SIMPLIFIED PATHS
        self.sliced_path = matic_paths.SLICED_DIR
        self.uploads_path = matic_paths.UPLOADS_DIR
        self.log_path = matic_paths.LOG_DIR
        # Live print log lives in tmpfs to avoid wearing the SD card; it is
        # copied to self.log_path exactly once when the print finishes.
        # /dev/shm is a world-writable tmpfs (1777) on Pi OS — unlike /run,
        # which is root-only, the MATIC user can create files here directly.
        self.tmpfs_log_path = '/dev/shm/matic_logs'

        # Create directories if they don't exist
        os.makedirs(self.uploads_path, exist_ok=True)
        os.makedirs(self.log_path, exist_ok=True)
        try:
            os.makedirs(self.tmpfs_log_path, exist_ok=True)
        except Exception as e:
            logger.error(f"Could not create tmpfs log dir {self.tmpfs_log_path}: {e}")

        # Config files for PrusaSlicer
        self.config_json_file = os.path.join(self.sliced_path, 'config.json')
        self.config_ini_file = os.path.join(self.sliced_path, 'config.ini')

        # Print log file handler
        self.print_log_file = None
        self.print_log_handler = None
        # Path of the active tmpfs log; set by start_print_log, cleared by close_print_log
        self.print_log_tmpfs_path = None
        # Final SD destination for the active log; set by start_print_log
        self.print_log_final_path = None
        # Cached print stats — read from SD once at startup, written back once per print
        self._cached_total_prints = None
        self._cached_total_print_minutes = None
        # Last severity recorded to local diagnostics history; used by the
        # 30-min timer to detect transitions and force an immediate row.
        self._last_recorded_severity = None
        
        # List to store found ZIP files
        self.zip_files = []
        
        # Print parameters (to be loaded from config files)
        self.layer_height = 0.0
        self.bottom_layer_count = 0
        self.bottom_exposure_time = 0.0
        self.normal_exposure_time = 0.0
        self.lift_height = 0.0
        self.lift_speed = 0.0
        self.drop_speed = 0.0
        self.total_layers = 0
        self.actual_slice_count = 0  # NEW: actual number of slice files found
        self.resolution_x = 0
        self.resolution_y = 0
        
        # PNG file base name (detected from extracted files)
        self.png_base_name = ""

        # Resin heating state
        self.is_heating = False
        self.should_stop_heating = False
        self.heating_time = 300
        
        # Cleaning state
        self.is_cleaning = False
        self.should_stop_cleaning = False
        self.cleaning_time = 15

        # Print state
        self.is_printing = False
        # True while a zip is being extracted into Sliced/ — /api/print/start
        # refuses during this window (a print started mid-extraction would
        # read a half-cleared / half-written layer set).
        self.is_extracting = False
        self.is_stopping = False
        self.is_paused = False
        self.pause_requested = False
        self.pause_processing = False
        self.current_layer = 0
        self.should_stop = False
        self.is_moving = False  # NEW: Track if platform is currently moving
        self.estimated_print_time = "Unknown"
        self.estimated_print_seconds = 0       
        
        # LAYER SKIPPING: Number of layers skipped at start
        self.layers_skipped = 0
        
        # Display driver (initialized in setup_display)
        self.display = None
        # True once a usable LCD panel is up; set False when create_display()
        # falls back to the headless NullDisplay (no panel, or none accepted a
        # mode). Gates slicing/printing and drives the 'Screen not detected' UI.
        self.screen_connected = True
        self.current_image = None
        
        # Status message for web UI
        self.status_message = "Ready"
        
        # Temperature monitoring
        self.temp_warning_threshold = 75.0  # Celsius - warning threshold
        self.temp_critical_threshold = 85.0  # Celsius - critical threshold
        self.current_temperature = 0.0
        self.last_temp_warning_logged = 0.0  # Track last warned temperature
        
        # Lock for thread safety
        self.lock = threading.Lock()
        
        # Flask app
        self.flask_thread = None
        self.app = Flask(__name__)
        
        # Temporary STL files storage for material selection
        self.temp_stl_files = {}
        
        # Setup. On an integration box with no printer wiring / no GPIO stack
        # (MATIC_ALLOW_NO_SCREEN=1), a GPIO failure must NOT abort the web
        # server — hardware routes simply no-op there. Real printers keep the
        # unguarded behaviour (a GPIO failure is fatal, as it should be).
        try:
            self.setup_gpio()
        except Exception as gpio_err:
            if os.environ.get('MATIC_ALLOW_NO_SCREEN') == '1':
                logger.warning(f"GPIO unavailable ({gpio_err}) — running "
                               "without printer hardware (integration mode)")
            else:
                raise
        self.load_calibration()
        
        # Design Portal slice-in-progress state.
        # is_design_slicing: True while we hold the serial lock for any
        #   design-portal slice (queued OR actively slicing). Print/Heat
        #   are locked out the whole time.
        # design_slice_phase: 'idle' | 'queued' | 'slicing'.
        # The remaining design_slice_* fields are structured data the
        # dentist UI uses to render a clean modal label without parsing
        # free-text.
        self.is_design_slicing = False
        self.design_slice_phase = 'idle'
        self.design_slicing_label = ''
        self.design_slice_file_count = 0
        self.design_slice_material = ''
        self.design_slice_patient = ''
        # Serialize Design Portal slice calls so multiple ingests (one per
        # material group) take turns instead of racing the slicer's
        # internal lock. The slicer has its own lock + queue but the queue
        # isn't auto-drained, so without this all groups except the first
        # would silently no-op.
        self._design_slicing_serial_lock = threading.Lock()

        # Initialize MATIC Cure
        self.cure = None
        if MaticCure is not None:
            try:
                self.cure = MaticCure()
                logger.info("MATIC Cure initialized")
            except Exception as e:
                logger.error(f"Failed to initialize MATIC Cure: {e}")
                self.cure = None
        
        self.setup_routes()
        
        # Scan for existing files
        self.find_zip_files()
                
        # Start network discovery service
        try:
            start_discovery_service(discovery_port=5005, web_port=self.port)
            logger.info("Discovery service started")
        except Exception as e:
            logger.error(f"Discovery service failed: {e}")

        # Start the local diagnostics persistence timer (30 min cadence,
        # plus immediate writes on severity transitions). Daemon thread so
        # it dies with the process.
        try:
            t = threading.Thread(
                target=self._diagnostics_timer_loop,
                name='diagnostics-timer',
                daemon=True,
            )
            t.start()
            logger.info("Diagnostics timer thread started")
        except Exception as e:
            logger.error(f"Failed to start diagnostics timer: {e}")

        # Start MATIC Core Mode -- a separate Flask app on port 5002,
        # password-protected, never advertised by ip_service. This is the
        # MATIC team's internal diagnostic UI; customers should not use it.
        try:
            t = threading.Thread(
                target=core_mode.start_core_mode,
                name='core-mode',
                daemon=True,
            )
            t.start()
            logger.info(f"Core Mode started on port {core_mode.CORE_PORT}")
        except Exception as e:
            logger.error(f"Failed to start Core Mode: {e}")

    def get_current_ip_address(self):
        """Get the current IP address (not cached) - for dynamic network changes"""
        try:
            # Method 1: Using hostname -I (most reliable on Pi)
            result = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                ips = result.stdout.strip().split()
                for ip in ips:
                    if ip and not ip.startswith('127.') and re.match(r'^(\d{1,3}\.){3}\d{1,3}$', ip):
                        return ip
            
            # Method 2: Using socket connection (fallback)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
                
        except Exception as e:
            logger.error(f"Error getting current IP: {e}")
            return self.ip_address if hasattr(self, 'ip_address') else "0.0.0.0"

    # Routes that are refused with HTTP 403 when the license is disabled
    # by the fleet admin. Any route that *initiates* new motion, heating,
    # slicing, or file ingestion belongs here. Routes that only observe
    # state (/api/status, /api/health, /api/*/status) or stop an already-
    # running operation (/api/print/stop, /api/*/stop, safe-shutdown) are
    # deliberately NOT listed — a disabled printer must still be able to
    # finish or abort whatever is currently happening. The customer UI
    # also hides behind a full-screen overlay, so this gate is mostly
    # defense-in-depth against a curl-level caller.
    _LICENSE_GATED_PATHS = frozenset({
        '/api/print/start',
        '/api/motor/toggle',
        '/api/move/down',
        '/api/heating/start',
        '/api/cleaning/start',
        '/api/cure/start',
        '/api/cure/almonding',
        '/api/calibration/move-to-sensor',
        '/api/calibration/move-to-home',
        '/api/slice-with-material',
        '/api/slice-excluded-models',
        '/api/upload',
        '/api/upload-multiple-stl',
        '/api/bundles/upload',
        '/api/bundles/select',
        '/api/bundles/delete',
    })

    @classmethod
    def _is_license_disabled(cls):
        """Return True only when the cached license file explicitly says
        'disabled'. Missing file, unreadable file, or a stale mtime
        (cloud agent stopped, no network to api.matic-3d.com) all read
        as active — a customer whose internet is down should never see
        a license-disabled overlay. Only an explicit admin disable from
        the fleet dashboard triggers the lockout."""
        license_file = matic_paths.LICENSE_STATUS
        try:
            with open(license_file) as f:
                return f.read().strip() == 'disabled'
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Design Portal helpers
    # ------------------------------------------------------------------
    def _read_device_token(self):
        """Return the X-Device-Token persisted by the cloud agent, or None."""
        try:
            with open(matic_paths.DEVICE_JSON) as f:
                return json.load(f).get('device_token')
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    _design_origins_lock = threading.Lock()

    def _read_design_origins(self):
        try:
            with open(DESIGN_ORIGINS_FILE) as f:
                d = json.load(f)
                return d if isinstance(d, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _write_design_origins(self, origins):
        """Atomic write: tmp + rename. Caller must hold _design_origins_lock."""
        tmp = DESIGN_ORIGINS_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(origins, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, DESIGN_ORIGINS_FILE)

    def _record_design_origin(self, filename, job_id, patient_name, kind=None,
                              is_sliced_output=False, material_name=None):
        """Mark a file in Uploads/ as having come from the Design Portal.
        Idempotent. Used by the cloud-agent ingest path and by the
        post-slice hook that propagates the marker to sliced outputs."""
        with self._design_origins_lock:
            origins = self._read_design_origins()
            origins[filename] = {
                'job_id': int(job_id),
                'patient_name': str(patient_name)[:200],
                'ingested_at': datetime.utcnow().isoformat(),
                'kind': kind,
                'is_sliced_output': bool(is_sliced_output),
                'material_name': material_name,
                # needs_material is True when the lab's chosen material
                # was not in the printer's current bundle at ingest time.
                # The dentist UI surfaces a "Pick material" link so the
                # file can be sliced manually.
                'needs_material': False,
                'requested_material': None,
            }
            self._write_design_origins(origins)

    def _mark_design_needs_material(self, filename, requested=None):
        """Set the needs_material flag on an already-recorded origin entry.
        Called when an STL ingests with a material name that isn't in the
        printer's currently-loaded bundle."""
        with self._design_origins_lock:
            origins = self._read_design_origins()
            entry = origins.get(filename)
            if entry is None:
                return
            entry['needs_material'] = True
            entry['requested_material'] = requested
            origins[filename] = entry
            self._write_design_origins(origins)

    def _lookup_design_origin(self, filename):
        return self._read_design_origins().get(filename)

    def _is_busy_for_design_slice(self):
        """True if the printer is doing something the design slice should
        wait for. We treat slicing as the only thing that's safe to
        overlap with itself (other design-portal slices serialize via
        _design_slicing_serial_lock); everything else makes us wait."""
        try:
            if self.is_printing or self.is_paused:
                return True
            if self.is_heating:
                return True
            if self.is_cleaning:
                return True
            if self.is_moving:
                return True
            if self.cure is not None and getattr(self.cure, 'is_curing', False):
                return True
        except Exception:
            return False
        return False

    def _cleanup_design_source_stls(self, filenames):
        """Remove the source STL files (and their registry entries) after
        a successful design-portal slice. Called from the slice _bg thread
        once the sliced output ZIP has been written. Without this the
        dentist sees both the sliced ZIP and the original STLs in the
        queue, which is confusing and bloats the 10-file LRU cap."""
        with self._design_origins_lock:
            origins = self._read_design_origins()
            changed = False
            for fn in filenames:
                # Drop the file from disk best-effort.
                try:
                    p = os.path.join(self.uploads_path, fn)
                    if os.path.realpath(p).startswith(
                            os.path.realpath(self.uploads_path) + os.sep):
                        if os.path.isfile(p):
                            os.remove(p)
                            logger.info(
                                f"design_ingest: cleaned up source STL {fn}"
                            )
                except OSError as e:
                    logger.warning(
                        f"design_ingest: cannot remove source STL {fn}: {e}"
                    )
                # Drop the registry entry for the source STL. The sliced
                # output ZIP keeps its own entry (recorded separately).
                if fn in origins:
                    del origins[fn]
                    changed = True
            if changed:
                self._write_design_origins(origins)

    def _unique_upload_filename(self, filename):
        """Return a filename that does not collide with anything already in
        Uploads/. If the requested name is free, returns it unchanged;
        otherwise appends `_2`, `_3`, ... before the extension until a free
        name is found. Callers MUST pass an already-sanitized filename
        (e.g. the result of secure_filename)."""
        if not filename:
            return filename
        target = os.path.join(self.uploads_path, filename)
        if not os.path.exists(target):
            return filename
        base, ext = os.path.splitext(filename)
        n = 2
        while True:
            candidate = f"{base}_{n}{ext}"
            if not os.path.exists(os.path.join(self.uploads_path, candidate)):
                return candidate
            n += 1

    def _design_ingest_secret(self):
        """Return the shared secret used to authenticate loopback POSTs from
        the cloud agent to /api/design/_ingest. Generated lazily on first
        read; permission 0600. The cloud agent reads this same file."""
        try:
            with open(DESIGN_INGEST_SECRET_FILE) as f:
                token = f.read().strip()
                if token:
                    return token
        except (FileNotFoundError, OSError):
            pass
        # Generate, write 0600, and persist.
        token = secrets.token_hex(32)
        try:
            os.makedirs(os.path.dirname(DESIGN_INGEST_SECRET_FILE), exist_ok=True)
            tmp = DESIGN_INGEST_SECRET_FILE + '.tmp'
            with open(tmp, 'w') as f:
                f.write(token)
            os.chmod(tmp, 0o600)
            os.replace(tmp, DESIGN_INGEST_SECRET_FILE)
        except OSError as e:
            logger.error(f"Failed to persist design ingest secret: {e}")
        return token

    def setup_routes(self):
        """Set up the Flask routes for the web interface"""

        @self.app.before_request
        def _enforce_license_gate():
            if request.path in self._LICENSE_GATED_PATHS and self._is_license_disabled():
                return jsonify({
                    'error': 'license_disabled',
                    'message': 'License disabled — contact MATIC support team',
                }), 403

        @self.app.route('/')
        def index():
            return load_find_printer_template()
        
        @self.app.route('/control')
        def control():
            return load_html_template()

        @self.app.route('/manifest.json')
        def pwa_manifest():
            """PWA manifest. The Windows installer creates a Chrome --app
            shortcut directly; this endpoint is for power users who want
            Chrome / Edge's in-browser 'Install app' button as well."""
            return jsonify({
                'name': 'MATIC Printer',
                'short_name': 'MATIC',
                'description': 'MATIC 3D Printer Control',
                'start_url': '/control',
                'scope': '/',
                'display': 'standalone',
                'orientation': 'any',
                'background_color': '#e8e8ed',
                'theme_color': '#c0392b',
                'icons': [
                    {'src': '/icon-192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
                    {'src': '/icon-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
                ],
            })

        @self.app.route('/icon-192.png')
        def pwa_icon_192():
            from flask import Response
            return Response(_generate_pwa_icon(192),
                            mimetype='image/png',
                            headers={'Cache-Control': 'public, max-age=86400'})

        @self.app.route('/icon-512.png')
        def pwa_icon_512():
            from flask import Response
            return Response(_generate_pwa_icon(512),
                            mimetype='image/png',
                            headers={'Cache-Control': 'public, max-age=86400'})

        @self.app.route('/api/test')
        def test_route():
            return jsonify({'status': 'Flask routes are working'})

        @self.app.route('/api/health')
        def get_health():
            """Lightweight health snapshot for the customer UI banner.
            Polled every 30s by the browser. Computes severity locally so
            this works even when the Pi has no internet. Also surfaces
            the cached license_status that cloud_agent writes after every
            heartbeat — when an admin disables a printer in the fleet
            dashboard, the customer banner picks it up on the next poll."""
            try:
                diag = health.collect_diagnostics()
                severity = health.severity_from_diagnostics(diag)
                issue = health.primary_issue(diag)
                return jsonify({
                    'severity': severity,
                    'issue': issue,
                    'license_disabled': self._is_license_disabled(),
                    'screen_connected': self.screen_connected,
                })
            except Exception as e:
                logger.error(f"/api/health failed: {e}")
                return jsonify({'severity': 'green', 'issue': None,
                                'license_disabled': False,
                                'screen_connected': getattr(self, 'screen_connected', True)})

        @self.app.route('/api/credits')
        def get_credits():
            """Return the dentist's Design-Portal credit balance, refreshed
            by cloud_agent on every heartbeat. The customer UI uses this to
            paint a chip next to the Design Portal toggle.

            Response shapes:
              {'available': true, 'balance': N, 'client_name': '...', 'as_of': '...'}
              {'available': true, 'balance': null, 'as_of': '...'}        -- no client linked
              {'available': false}                                         -- no cache yet
            """
            cache_path = matic_paths.CREDITS_JSON
            try:
                with open(cache_path) as f:
                    doc = json.load(f)
            except (OSError, ValueError):
                return jsonify({'available': False})
            return jsonify({
                'available': True,
                'balance': doc.get('balance'),
                'client_name': doc.get('client_name'),
                'as_of': doc.get('as_of'),
            })

        # ---- Pending update endpoints ---------------------------------
        #
        # cloud_agent.py is responsible for download + verify + stage of
        # an update. The result lives at /home/MATIC/MATIC_OS_DEV/.device/
        # pending_update.json (metadata) + pending_update.tar.gz (the
        # verified payload). main.py exposes the metadata to the UI via
        # /api/pending-update so the customer banner can show "Update
        # available" with build info and release notes. When the
        # operator clicks Install Now in the UI, the JS POSTs to
        # /api/install-pending-update which touches a consent file that
        # cloud_agent.py picks up on its next loop iteration -- the
        # actual install runs in the cloud agent process, not here.

        @self.app.route('/api/pending-update', methods=['GET'])
        def get_pending_update():
            """Return the pending update metadata if cloud_agent has staged
            one, otherwise {available: false}. Polled every 30s by the
            customer banner alongside /api/health. Cheap (one stat + one
            small JSON read)."""
            pending_path = matic_paths.PENDING_UPDATE_JSON
            try:
                if not os.path.exists(pending_path):
                    return jsonify({'available': False})
                with open(pending_path) as f:
                    data = json.load(f) or {}
                if not data:
                    return jsonify({'available': False})
                # Tell the UI whether the operator has already consented
                # so the banner can flip to "installing" state.
                consent_path = matic_paths.PENDING_UPDATE_CONSENT
                data['consent_given'] = os.path.exists(consent_path)
                data['available'] = True
                return jsonify(data)
            except Exception as e:
                logger.error(f"/api/pending-update failed: {e}")
                return jsonify({'available': False})

        @self.app.route('/api/defer-pending-update', methods=['POST'])
        def post_defer_pending_update():
            """Operator clicked Later. Write a defer marker file containing
            the current boot id. cloud_agent.apply_staged_update() will
            install the staged update on the next loop iteration where
            the current boot id is different (i.e. the printer has been
            power-cycled). Until then the staged update sits idle on
            disk -- no time-based deadline, no nagging."""
            pending_path = matic_paths.PENDING_UPDATE_JSON
            if not os.path.exists(pending_path):
                return jsonify({
                    'status': 'refused',
                    'reason': 'no pending update',
                }), 404

            try:
                with open('/proc/sys/kernel/random/boot_id') as f:
                    boot_id = f.read().strip()
            except Exception as e:
                logger.error(f"Failed to read boot_id for defer: {e}")
                return jsonify({'status': 'error', 'reason': 'cannot read boot_id'}), 500

            defer_path = matic_paths.PENDING_UPDATE_DEFER
            try:
                with open(defer_path, 'w') as f:
                    f.write(boot_id + '\n')
                logger.info(f"Operator deferred pending update to next boot (boot_id={boot_id[:8]})")
                return jsonify({'status': 'ok'})
            except Exception as e:
                logger.error(f"/api/defer-pending-update failed: {e}")
                return jsonify({'status': 'error', 'reason': str(e)}), 500

        @self.app.route('/api/install-pending-update', methods=['POST'])
        def post_install_pending_update():
            """Operator clicked Install Now. Touch the consent file that
            cloud_agent watches. cloud_agent will install the staged
            update on its next loop iteration (within ~30 seconds), but
            ONLY if the printer is also idle at that time -- the cloud
            agent has its own busy-state check as belt-and-suspenders.
            Refuses to write the consent file if the printer is currently
            busy, so a misbehaving UI cannot trigger an install during a
            print."""
            # is_curing is on self.cure (not directly on self) -- mirror
            # exactly what /api/status does to determine the busy flags.
            curing_now = bool(self.cure and self.cure.is_curing)
            if (self.is_printing or self.is_paused or self.is_moving
                    or self.is_heating or self.is_cleaning or curing_now):
                return jsonify({
                    'status': 'refused',
                    'reason': 'printer is busy',
                }), 409

            pending_path = matic_paths.PENDING_UPDATE_JSON
            if not os.path.exists(pending_path):
                return jsonify({
                    'status': 'refused',
                    'reason': 'no pending update',
                }), 404

            consent_path = matic_paths.PENDING_UPDATE_CONSENT
            try:
                # Touch the consent file with the current ISO timestamp
                # as content. cloud_agent only checks for existence, but
                # the timestamp is useful when debugging from the journal.
                with open(consent_path, 'w') as f:
                    f.write(datetime.now().isoformat() + '\n')
                logger.info("Operator consented to pending update install")
                return jsonify({'status': 'ok'})
            except Exception as e:
                logger.error(f"/api/install-pending-update failed: {e}")
                return jsonify({'status': 'error', 'reason': str(e)}), 500

        @self.app.route('/connect')
        def connect():
            return load_find_printer_template()
                
        @self.app.route('/api/ip')
        def get_server_ip():
            """Return the actual current IP address of this Pi"""
            return jsonify({'ip': self.get_current_ip_address(), 'port': self.port})

        @self.app.route('/api/files')
        def get_files():
            try:
                # Refresh file list
                self.find_zip_files()
                origins = self._read_design_origins()
                files_info = []
                for f in self.zip_files:
                    name = f['name']
                    info = {
                        'name': name,
                        'basename': os.path.splitext(name)[0],
                        'mtime': f.get('mtime'),
                    }
                    origin = origins.get(name)
                    if origin:
                        info['from_design_portal'] = True
                        info['design_job_id'] = origin.get('job_id')
                        info['design_patient_name'] = origin.get('patient_name')
                        info['design_material_name'] = origin.get('material_name')
                        info['design_needs_material'] = bool(origin.get('needs_material'))
                        info['design_requested_material'] = origin.get('requested_material')
                        info['design_is_sliced_output'] = bool(origin.get('is_sliced_output'))
                    else:
                        info['from_design_portal'] = False
                    # Smart Engine batches stay EDITABLE: double-clicking the
                    # row re-opens the batch group in the engine. Applies to
                    # engine-sliced and auto-sliced batches alike.
                    if engine_bridge is not None and engine_bridge.available():
                        q = engine_bridge.queue_info(name)
                        if q:
                            info['engine_batch'] = True
                            info['engine_group'] = q['group']
                    files_info.append(info)
                return jsonify(files_info)
            except Exception as e:
                logger.error(f"Error in /api/files route: {str(e)}")
                return jsonify({'error': str(e)}), 500
                
        @self.app.route('/api/materials')
        def get_materials():
            try:
                if not self.auto_slicer.available_materials:
                    self.auto_slicer.parse_materials_from_bundle()
                
                materials_info = []
                for material in self.auto_slicer.available_materials:
                    materials_info.append({
                        'name': material.name,
                        'exposure_time': material.exposure_time,
                        'initial_exposure_time': material.initial_exposure_time,
                        'x_rotation': material.x_rotation,
                        'material_colour': material.material_colour,
                        'material_print_speed': material.material_print_speed,
                        'material_notes': material.material_notes
                    })

                # Merged deploy: custom resins created in the Smart Engine's
                # profile editor are selectable for auto-slicing too — append
                # them in the dialog shape (cure recipe synthesized into the
                # notes format the cure UI parses).
                if engine_bridge is not None and engine_bridge.available():
                    have = {m['name'] for m in materials_info}
                    for p in engine_bridge.custom_profiles():
                        prof = engine_bridge.normalize_profile(p)
                        if prof['name'] in have:
                            continue
                        materials_info.append({
                            'name': prof['name'],
                            'exposure_time': prof['exposure_time'],
                            'initial_exposure_time': prof['initial_exposure_time'],
                            'x_rotation': prof['x_rotation'],
                            'material_colour': prof['color'],
                            'material_print_speed': prof['print_speed'],
                            'material_notes': (f"C={prof['cure_uv_s']}\n"
                                               f"R={prof['cure_rotation_s']}"),
                        })

                return jsonify({'materials': materials_info})
            except Exception as e:
                logger.error(f"Error in /api/materials route: {str(e)}")
                return jsonify({'materials': [], 'error': str(e)})

        # ==================== MATIC Smart Engine Routes ====================
        # Present only in the merged deploy (Smart Engine in-process). The
        # control UI probes /api/engine/available to decide whether to offer
        # the Smart Engine option in the material dialog and the queue rows.

        @self.app.route('/api/engine/available')
        def engine_available():
            ok = engine_bridge is not None and engine_bridge.available()
            return jsonify({'available': ok})

        @self.app.route('/api/engine/handoff', methods=['POST'])
        def engine_handoff():
            """Hand the just-uploaded STLs to the Smart Engine: stage them
            under a one-shot token and return the deep link the browser
            opens. Clears the pending upload exactly like a dialog Cancel."""
            if engine_bridge is None or not engine_bridge.available():
                return jsonify({'success': False,
                                'message': 'Smart Engine not available'}), 503
            try:
                data = request.json or {}
                temp_stl_id = data.get('temp_stl_id')
                if not temp_stl_id or temp_stl_id not in self.temp_stl_files:
                    return jsonify({'success': False,
                                    'message': 'STL file not found or expired'}), 400
                info = self.temp_stl_files[temp_stl_id]
                if info.get('is_multiple', False):
                    items = list(zip(info['paths'],
                                     info.get('filenames') or
                                     [os.path.basename(p) for p in info['paths']]))
                else:
                    items = [(info['path'],
                              info.get('filename') or
                              os.path.basename(info['path']))]
                token = engine_bridge.stage_handoff(items)
                # staged — drop the temp upload + its queue placeholder
                for p, _n in items:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
                del self.temp_stl_files[temp_stl_id]
                self.auto_slicer.queued_operations = [
                    op for op in self.auto_slicer.queued_operations
                    if op['id'] != temp_stl_id
                ]
                return jsonify({'success': True, 'token': token,
                                'url': f'/engine/?handoff={token}'})
            except Exception as e:
                logger.error(f"Error in engine handoff: {str(e)}")
                return jsonify({'success': False, 'message': str(e)}), 500

        @self.app.route('/api/autoslice-layer', methods=['GET', 'POST'])
        def autoslice_layer():
            """The auto-slicer's layer height (50 or 100 micron) — the
            selector that replaced Bundle Management in Settings."""
            if engine_bridge is None or not engine_bridge.available():
                return jsonify({'layer_um': 100, 'available': False})
            if request.method == 'POST':
                try:
                    v = engine_bridge.set_autoslice_layer_um(
                        (request.json or {}).get('layer_um'))
                    return jsonify({'success': True, 'layer_um': v})
                except (TypeError, ValueError) as e:
                    return jsonify({'success': False, 'message': str(e)}), 400
            return jsonify({'layer_um': engine_bridge.autoslice_layer_um(),
                            'available': True})

        @self.app.route('/api/cancel-autoslice', methods=['POST'])
        def cancel_autoslice():
            """Abort the Smart Engine auto-slice currently running (the
            slicing modal's Cancel button)."""
            if engine_bridge is None or not engine_bridge.available():
                return jsonify({'success': False,
                                'message': 'Smart Engine not available'}), 503
            return jsonify({'success': bool(engine_bridge.cancel_autoslice())})

        # ==================== MATIC Cure Routes ====================
        @self.app.route('/api/cure/start', methods=['POST'])
        def start_cure():
            try:
                if self.cure is None:
                    return jsonify({'success': False, 'message': 'Cure system not initialized'})
                if self.cure.is_curing:
                    return jsonify({'success': False, 'message': 'Cure already in progress'})
                
                data = request.get_json()
                material_name = data.get('material_name')
                material_notes = data.get('material_notes', '')
                
                if not material_notes:
                    return jsonify({'success': False, 'message': 'No cure parameters found'})
                
                cure_time, rotation_time = self.cure.parse_cure_parameters(material_notes)
                
                if cure_time <= 0 or rotation_time <= 0:
                    return jsonify({'success': False, 'message': 'Invalid cure parameters in material_notes'})
                
                success, message = self.cure.start_cure(material_name, cure_time, rotation_time)
                return jsonify({'success': success, 'message': message})
                
            except Exception as e:
                logger.error(f"Error starting cure: {e}")
                return jsonify({'success': False, 'message': str(e)})
        
        @self.app.route('/api/cure/almonding', methods=['POST'])
        def start_almonding():
            try:
                if self.cure is None:
                    return jsonify({'success': False, 'message': 'Cure system not initialized'})
                if self.cure.is_curing:
                    return jsonify({'success': False, 'message': 'Cure already in progress'})

                success, message = self.cure.start_almonding()
                return jsonify({'success': success, 'message': message})
            except Exception as e:
                logger.error(f"Error starting almonding: {e}")
                return jsonify({'success': False, 'message': str(e)})

        @self.app.route('/api/cure/stop', methods=['POST'])
        def stop_cure():
            try:
                if self.cure is None:
                    return jsonify({'success': False, 'message': 'Cure system not initialized'})
                
                success, message = self.cure.stop_cure()
                return jsonify({'success': success, 'message': message})
            except Exception as e:
                logger.error(f"Error stopping cure: {e}")
                return jsonify({'success': False, 'message': str(e)})
        
        @self.app.route('/api/cure/status')
        def get_cure_status():
            try:
                if self.cure is None:
                    return jsonify({'is_curing': False, 'cure_progress': 0, 'cure_status': 'Unavailable', 'cure_material': None})

                return jsonify(self.cure.get_cure_status())
            except Exception as e:
                logger.error(f"Error getting cure status: {e}")
                return jsonify({'error': str(e)})

        # ==================== Design Portal Routes ====================
        # Pi proxies upload + history + queue to api.matic-3d.com using this
        # printer's persisted X-Device-Token. The cloud agent owns the
        # download/ingest side via the internal _ingest endpoint below.
        # PII rule: patient_name flows through these endpoints by necessity
        # but is NEVER written to the journal -- log job_id only.

        @self.app.route('/api/design/upload', methods=['POST'])
        def design_upload():
            """Accept one or more scan files from the dentist UI, bundle them
            into a single zip locally, and forward the zip to the cloud as
            one Design Portal job. Bundling on the Pi keeps the cloud schema
            simple (one scan_filename per job) and gives the lab a single
            archive to drop into their CAD tool."""
            token = self._read_device_token()
            if not token:
                return jsonify({'success': False,
                                'message': 'printer not yet registered with cloud (no token)'}), 503

            uploads = request.files.getlist('files')
            if not uploads:
                # Backward-compat with the single-file form field name.
                if 'file' in request.files:
                    uploads = [request.files['file']]
            uploads = [f for f in uploads if f and (f.filename or '').strip()]
            if not uploads:
                return jsonify({'success': False, 'message': 'pick at least one file'}), 400
            if len(uploads) > 30:
                return jsonify({'success': False, 'message': 'too many files (max 30)'}), 400

            patient_name = (request.form.get('patient_name') or '').strip()
            if not patient_name:
                return jsonify({'success': False, 'message': 'patient name required'}), 400
            if len(patient_name) > 200:
                return jsonify({'success': False, 'message': 'patient name too long'}), 400

            note = (request.form.get('note') or '').strip()
            if len(note) > 2000:
                return jsonify({'success': False, 'message': 'note too long'}), 400

            # Build the zip in /tmp so we don't fight tmpfs limits or hold
            # the request body in memory. Total cap matches the cloud cap.
            MAX_TOTAL = 250 * 1024 * 1024
            tmp_zip = tempfile.NamedTemporaryFile(
                prefix='design_upload_', suffix='.zip', delete=False
            )
            tmp_zip_path = tmp_zip.name
            tmp_zip.close()
            try:
                total = 0
                seen_names = set()
                with zipfile.ZipFile(tmp_zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for f in uploads:
                        raw = (f.filename or '').strip()
                        # Sanitise INSIDE the archive so the lab gets safe
                        # filenames even if the browser sends paths.
                        inner = secure_filename(raw) or 'file'
                        # Avoid duplicates by suffixing _2, _3, ...
                        base = inner
                        n = 2
                        while inner in seen_names:
                            stem, ext = os.path.splitext(base)
                            inner = f'{stem}_{n}{ext}'
                            n += 1
                        seen_names.add(inner)
                        # Stream into the zip with a running size check.
                        f.stream.seek(0)
                        with zf.open(inner, 'w') as out:
                            while True:
                                chunk = f.stream.read(64 * 1024)
                                if not chunk:
                                    break
                                total += len(chunk)
                                if total > MAX_TOTAL:
                                    raise RuntimeError('total scan size exceeds 250 MB')
                                out.write(chunk)
                if total == 0:
                    return jsonify({'success': False, 'message': 'all files were empty'}), 400

                # Compose a meaningful filename for the lab UI.
                safe_patient = secure_filename(patient_name) or 'scan'
                cloud_filename = f'{safe_patient}.zip'

                with open(tmp_zip_path, 'rb') as f:
                    try:
                        post_data = {'patient_name': patient_name}
                        if note:
                            post_data['note'] = note
                        resp = requests.post(
                            f'{DESIGN_API_BASE}/api/design/upload/{self.device_serial}',
                            headers={'X-Device-Token': token},
                            files={'file': (cloud_filename, f, 'application/zip')},
                            data=post_data,
                            timeout=DESIGN_API_UPLOAD_TIMEOUT_S,
                        )
                    except requests.RequestException as e:
                        logger.error(f"design_upload: network error: {e}")
                        return jsonify({'success': False,
                                        'message': 'cloud unreachable -- try again'}), 502

                if resp.status_code == 200:
                    body = resp.json()
                    logger.info(
                        f"design_upload: created job {body.get('job_id')} "
                        f"({len(uploads)} files, {total} bytes zipped)"
                    )
                    return jsonify({'success': True, **body})
                # The cloud (or its upstream nginx) usually responds with
                # JSON, but a 413 from nginx itself is plain HTML. Decode
                # carefully so the dentist UI gets a useful message.
                if resp.status_code == 413:
                    err = (f'upload rejected -- file too large ({total // 1024 // 1024} MB) '
                           f'or proxy limit too tight')
                else:
                    try:
                        err = resp.json().get('error', f'cloud returned {resp.status_code}')
                    except ValueError:
                        snippet = resp.text[:200].replace('\n', ' ').strip()
                        err = f'cloud returned {resp.status_code}: {snippet[:80]}'
                logger.warning(f"design_upload: cloud returned {resp.status_code}: {err}")
                return jsonify({'success': False, 'message': err}), resp.status_code

            except RuntimeError as e:
                return jsonify({'success': False, 'message': str(e)}), 413
            except Exception as e:
                logger.error(f"design_upload: failed: {e}")
                return jsonify({'success': False, 'message': 'upload failed'}), 500
            finally:
                try:
                    os.remove(tmp_zip_path)
                except OSError:
                    pass

        @self.app.route('/api/design/history', methods=['GET'])
        def design_history():
            token = self._read_device_token()
            if not token:
                return jsonify({'jobs': [], 'unregistered': True}), 200
            try:
                resp = requests.get(
                    f'{DESIGN_API_BASE}/api/design/history/{self.device_serial}',
                    headers={'X-Device-Token': token},
                    timeout=DESIGN_API_TIMEOUT_S,
                )
            except requests.RequestException as e:
                logger.warning(f"design_history: network error: {e}")
                return jsonify({'jobs': [], 'offline': True}), 200
            if resp.status_code != 200:
                logger.warning(f"design_history: cloud returned {resp.status_code}")
                return jsonify({'jobs': [], 'cloud_error': resp.status_code}), 200
            return jsonify(resp.json())

        @self.app.route('/api/design/queue/<int:job_id>', methods=['POST'])
        def design_queue(job_id):
            """'Add to queue' from the dentist History modal. Tells the
            cloud to mark every result of this job as undelivered and the
            job as 'ready', then pokes the local cloud agent so it
            re-downloads the result files into Uploads/ on the next loop
            tick (no 5-minute wait)."""
            token = self._read_device_token()
            if not token:
                return jsonify({'success': False, 'message': 'no cloud token'}), 503
            try:
                resp = requests.post(
                    f'{DESIGN_API_BASE}/api/design/jobs/{job_id}/redeliver',
                    headers={'X-Device-Token': token},
                    timeout=DESIGN_API_TIMEOUT_S,
                )
            except requests.RequestException as e:
                logger.warning(f"design_queue: network error: {e}")
                return jsonify({'success': False,
                                'message': 'cloud unreachable -- try again'}), 502
            if resp.status_code != 200:
                try:
                    err = resp.json().get('error', 'queue failed')
                except ValueError:
                    err = 'queue failed'
                return jsonify({'success': False, 'message': err}), resp.status_code

            # Poke the local cloud agent so the redelivery happens within
            # ~30 seconds instead of waiting for the 5-minute interval.
            # Best-effort -- if the signal can't be sent the next regular
            # poll will still pick it up.
            try:
                # Match the cloud-agent process by basename: on deployed
                # printers it runs via the shim cloud_agent.pyc (-> .so), and
                # on the dev Pi as cloud_agent.py. r'cloud_agent\.py' matches
                # both (".pyc" contains ".py"). A rooted install path never
                # matched the deployed cmdline, so the poke silently no-op'd.
                pid_out = subprocess.run(
                    ['pgrep', '-f', r'cloud_agent\.py'],
                    capture_output=True, text=True, timeout=2,
                )
                pid_str = (pid_out.stdout or '').strip().splitlines()
                if pid_str:
                    os.kill(int(pid_str[0]), signal.SIGUSR1)
                    logger.info(
                        f"design_queue: poked cloud agent (pid={pid_str[0]}) "
                        f"to redeliver job {job_id}"
                    )
            except Exception as e:
                logger.warning(f"design_queue: could not poke cloud agent: {e}")

            logger.info(f"design_queue: job {job_id} flagged for redeliver")
            return jsonify({'success': True, **resp.json()})

        # Internal: cloud agent calls this on loopback after downloading
        # results. NEVER expose to LAN. Two-layer auth: 127.0.0.1-only at
        # the handler level, plus a shared secret in /home/MATIC/MATIC_OS_DEV/.device/.
        # Accepts a list of files for one job (single-kind: all STL or all
        # ZIP). For ZIP kind we just register origins; for STL we register
        # origins AND kick off the existing multi-STL slicer with the
        # lab-chosen material. If the material isn't in the printer's
        # current bundle the STLs are left in Uploads/ with a needs_material
        # flag for the dentist to resolve manually.
        @self.app.route('/api/design/_ingest', methods=['POST'])
        def design_ingest():
            if request.remote_addr not in ('127.0.0.1', '::1'):
                logger.warning(
                    f"design_ingest: rejected non-loopback request from "
                    f"{request.remote_addr}"
                )
                return jsonify({'error': 'loopback only'}), 403
            sent = (request.headers.get('X-Ingest-Secret') or '').strip()
            expected = self._design_ingest_secret()
            if not sent or not secrets.compare_digest(sent, expected):
                return jsonify({'error': 'bad secret'}), 401

            # No LCD panel -> sliced output can't be printed on this unit, so
            # don't silently slice a dentist's job for a headless printer.
            if not getattr(self, 'screen_connected', True):
                return jsonify({'status': 'no_screen',
                                'error': 'Screen not detected'}), 503

            data = request.get_json(silent=True) or {}
            job_id = data.get('job_id')
            patient_name = data.get('patient_name')
            kind = data.get('kind')
            material_name = (data.get('material_name') or '').strip() or None
            files = data.get('files') or []
            if (not isinstance(job_id, int) or not patient_name
                    or kind not in ('stl', 'zip')
                    or not isinstance(files, list) or not files):
                return jsonify({'error': 'missing fields'}), 400

            # Resolve every filename to a real path under Uploads/ before
            # doing any work. Refuse the whole batch on any path traversal
            # or missing file.
            paths = []
            uploads_real = os.path.realpath(self.uploads_path)
            for fn in files:
                if not isinstance(fn, str) or not fn:
                    return jsonify({'error': 'bad filename'}), 400
                target = os.path.join(self.uploads_path, fn)
                if not os.path.realpath(target).startswith(uploads_real + os.sep):
                    return jsonify({'error': 'path traversal'}), 400
                if not os.path.isfile(target):
                    return jsonify({'error': f'file not in Uploads/: {fn}'}), 404
                paths.append(target)

            # Always record origin first so the badge shows even if
            # downstream slicing fails. needs_material defaults to False;
            # we set it to True below if STL with unknown material.
            for fn in files:
                self._record_design_origin(
                    fn, job_id, patient_name, kind=kind,
                    material_name=material_name,
                )

            if kind == 'zip':
                logger.info(
                    f"design_ingest: registered job {job_id} ({len(files)} ZIPs)"
                )
                # Refresh the file list so /api/files immediately returns
                # the new entries.
                self.find_zip_files()
                return jsonify({'status': 'ok', 'kind': 'zip',
                                'placed': len(files)})

            # STL kind. Validate material is in the current bundle.
            available = []
            try:
                if not self.auto_slicer.available_materials:
                    self.auto_slicer.parse_materials_from_bundle()
                available = [m.name for m in self.auto_slicer.available_materials]
            except Exception as e:
                logger.error(f"design_ingest: cannot read materials: {e}")

            material_ok = bool(material_name) and material_name in available
            if not material_ok:
                # Mark each file as needing manual material selection so
                # the UI can prompt the dentist.
                for fn in files:
                    self._mark_design_needs_material(
                        fn, requested=material_name or None,
                    )
                logger.warning(
                    f"design_ingest: job {job_id} STL needs_material "
                    f"(requested={material_name!r}, available={available})"
                )
                self.find_zip_files()
                return jsonify({
                    'status': 'needs_material',
                    'requested_material': material_name,
                    'available_materials': available,
                    'placed': len(files),
                })

            # Trigger the slicer on a background thread so the cloud
            # agent's HTTP call returns immediately. Slicing can take many
            # minutes; the dentist's existing /api/slicing-progress UI
            # will surface progress.
            def _bg():
                # Serialize against other in-flight Design Portal slice
                # threads (one per material group). The slicer's own lock
                # would queue concurrent calls but never drains them, so
                # without this groups 2..N would silently no-op.
                with self._design_slicing_serial_lock:
                    # Wait for the printer to be idle. While waiting we
                    # set phase='queued' so the dentist UI shows the
                    # passive banner (not the modal). is_design_slicing
                    # stays True the whole time so Print/Heat are locked.
                    self.is_design_slicing = True
                    while self._is_busy_for_design_slice():
                        self.design_slice_phase = 'queued'
                        self.design_slicing_label = (
                            f"Design Portal slice for {patient_name} "
                            f"({material_name}) will start after finishing "
                            f"the current operation"
                        )
                        time.sleep(5)

                    # Printer is idle. Flip to 'slicing' phase so the
                    # dentist UI replaces the banner with the same
                    # blocking modal dialog the manual slice flow uses.
                    self.design_slice_phase = 'slicing'
                    self.design_slicing_label = (
                        f"Slicing {len(paths)} file(s) for "
                        f"{patient_name} ({material_name})"
                    )
                    self.design_slice_file_count = len(paths)
                    self.design_slice_material = material_name or ''
                    self.design_slice_patient = patient_name or ''
                    try:
                        if engine_bridge is not None and engine_bridge.available():
                            # Merged deploy: the Smart Engine auto-slices the
                            # Design Portal job in-process (Abu Layer pose +
                            # supports, panel-native resolution, one-family
                            # batches) — the same delegation as
                            # /api/slice-with-material; the old external
                            # slicer is not installed here.
                            out_names = engine_bridge.auto_slice(
                                paths, files, material_name)
                            ok = bool(out_names)
                            result = out_names[0] if out_names else None
                            out_zips = [z for z in (out_names or [])
                                        if isinstance(z, str)
                                        and z.lower().endswith('.zip')]
                        else:
                            if not self.auto_slicer.current_bundle:
                                logger.error(
                                    "design_ingest: no current bundle, cannot slice"
                                )
                                return
                            # Single-STL has its own dedicated function; the
                            # multi-STL function's len==1 shortcut passes
                            # output_name=None into a helper that crashes on
                            # os.path.join(uploads, None). Dispatch by length.
                            if len(paths) == 1:
                                single_ok, single_msg = self.auto_slicer.slice_stl_with_material(
                                    paths[0], None, material_name,
                                    original_filename=files[0],
                                )
                                ok = single_ok
                                # slice_stl_with_material returns the output
                                # filename in `single_msg` on success, an
                                # error string on failure.
                                result = single_msg if single_ok else None
                                if not single_ok:
                                    logger.error(
                                        f"design_ingest: single-STL slice failed "
                                        f"for job {job_id}: {single_msg}"
                                    )
                            else:
                                ok, result, _excluded = self.auto_slicer.slice_multiple_stl_with_material(
                                    paths, None, material_name,
                                    original_stl_names=files,
                                )
                            out_zips = ([result] if isinstance(result, str)
                                        and result.lower().endswith('.zip')
                                        else [])
                        if ok:
                            logger.info(
                                f"design_ingest: sliced job {job_id} -> {result}"
                            )
                            for out_zip in out_zips:
                                try:
                                    self._record_design_origin(
                                        out_zip, job_id, patient_name,
                                        kind='sliced', is_sliced_output=True,
                                        material_name=material_name,
                                    )
                                except Exception as e:
                                    logger.error(
                                        f"design_ingest: tag primary output failed: {e}"
                                    )
                            # Clean up the source STLs now that the sliced
                            # output is in Uploads/. Without this we leave
                            # N orphan STL rows in the dentist's queue.
                            try:
                                self._cleanup_design_source_stls(files)
                            except Exception as e:
                                logger.error(
                                    f"design_ingest: post-slice cleanup failed: {e}"
                                )
                            self.find_zip_files()
                        else:
                            logger.error(
                                f"design_ingest: slice failed for job {job_id}: {result}"
                            )
                    except Exception as e:
                        if (engine_bridge is not None
                                and isinstance(e, engine_bridge.AutoSliceCancelled)):
                            logger.info(
                                f"design_ingest: slice cancelled for job {job_id}"
                            )
                        else:
                            logger.error(
                                f"design_ingest: slice crashed for job {job_id}: {e}"
                            )
                    finally:
                        self.is_design_slicing = False
                        self.design_slice_phase = 'idle'
                        self.design_slicing_label = ''
                        self.design_slice_file_count = 0
                        self.design_slice_material = ''
                        self.design_slice_patient = ''

            threading.Thread(target=_bg, daemon=True,
                             name=f"design-slice-{job_id}").start()
            self.find_zip_files()
            return jsonify({
                'status': 'slicing',
                'material_name': material_name,
                'placed': len(files),
            })

        @self.app.route('/api/calibration/move-to-sensor', methods=['POST'])
        def move_to_sensor():
            try:
                # Safety checks
                if self.is_printing:
                    return jsonify({'success': False, 'message': 'Cannot calibrate while printing'})
                
                if self.is_heating:
                    return jsonify({'success': False, 'message': 'Cannot calibrate during resin heating'})
                
                if self.is_cleaning:
                    return jsonify({'success': False, 'message': 'Cannot calibrate during cleaning'})
                
                if self.is_moving:
                    return jsonify({'success': False, 'message': 'Platform is already moving'})
                
                # Make sure motor is enabled
                if not self.motor_enabled:
                    self.enable_motor()
                
                logger.info("Starting guided calibration - moving to sensor")
                self.status_message = "Calibration: Moving platform towards sensor..."
                
                # Call the existing sensor movement function
                success = self.move_down_until_sensor_trigger(max_distance_mm=150, speed_mm_min=1200)
                
                if success:
                    # Sensor was triggered - set this as zero position
                    self.current_position_mm = 0.0
                    self.position_changed = True
                    self.save_calibration()
                    
                    logger.info("Calibration: Sensor triggered - zero position set")
                    self.status_message = "Calibration: Zero position set at sensor trigger point"
                    
                    return jsonify({
                        'success': True, 
                        'message': 'Sensor triggered successfully. Zero position has been set.'
                    })
                else:
                    logger.warning("Calibration: Sensor not triggered within range")
                    self.status_message = "Calibration failed - sensor not triggered"
                    
                    return jsonify({
                        'success': False, 
                        'message': 'Sensor was not triggered within the maximum distance. Please check sensor and platform position.'
                    })
                    
            except Exception as e:
                logger.error(f"Error during sensor calibration: {str(e)}")
                self.status_message = f"Calibration error: {str(e)[:50]}"
                return jsonify({'success': False, 'message': f'Calibration error: {str(e)}'})

        @self.app.route('/api/calibration/move-to-home', methods=['POST'])
        def move_to_home():
            try:
                # Safety checks
                if self.is_printing:
                    return jsonify({'success': False, 'message': 'Cannot move to home while printing'})
                
                if self.is_heating:
                    return jsonify({'success': False, 'message': 'Cannot move to home during resin heating'})
                
                if self.is_cleaning:
                    return jsonify({'success': False, 'message': 'Cannot move to home during cleaning'})
                
                if self.is_moving:
                    return jsonify({'success': False, 'message': 'Platform is already moving'})
                
                # Make sure motor is enabled
                if not self.motor_enabled:
                    self.enable_motor()
                    
                # Dwell before direction change to let winding current decay
                logger.info("Waiting before direction change to protect driver...")
                time.sleep(0.5)
                
                logger.info("Moving up by 0.1mm to account for FEP film thickness")
                self.dir_pin.value = self.DIR_DOWN
                
                # Calculate steps for 0.1mm with gradual acceleration
                steps_01mm = int(0.1 * self.STEPS_PER_MM)
                target_delay = 0.0005  # Gentle speed for 0.1mm move (not max speed)
                start_delay = 0.002  # Start slow
                
                # Perform steps with ramp-up (no position tracking for this offset)
                for i in range(steps_01mm):
                    # Ramp up over all steps (only 40 steps for 0.1mm so ramp the whole way)
                    if i < steps_01mm:
                        ratio = i / steps_01mm
                        current_delay = start_delay - (start_delay - target_delay) * ratio
                    else:
                        current_delay = target_delay
                    
                    self.step_pin.on()
                    time.sleep(current_delay)
                    self.step_pin.off()
                    time.sleep(current_delay)
                    
                logger.info("Platform moved up by 0.1mm after screw tightening (not reflected in position tracking)")
                time.sleep(0.5)
                logger.info("Calibration: Moving platform to home position")
                self.status_message = "Calibration: Moving to home position..."
                
                # Move platform to home position (100mm below zero)
                self.move_to_position(HOME_POSITION, speed_mm_min=self.move_home_speed)
                
                # Save calibration after successful movement
                self.save_calibration()
                
                logger.info(f"Calibration complete: Platform at home position ({self.current_position_mm:.2f}mm)")
                self.status_message = f"Calibration complete - at home position ({abs(self.current_position_mm):.2f}mm)"
                
                return jsonify({
                    'success': True, 
                    'message': f'Calibration completed successfully. Platform is now at home position ({abs(self.current_position_mm):.2f}mm).'
                })
                
            except Exception as e:
                logger.error(f"Error moving to home position: {str(e)}")
                self.status_message = f"Error moving to home: {str(e)[:50]}"
                return jsonify({'success': False, 'message': f'Error moving to home position: {str(e)}'})
            
        @self.app.route('/api/slicing-progress')
        def get_slicing_progress():
            """Get detailed slicing progress for UI progress bar"""
            def _pending_excluded_payload():
                """If a previous slice produced an excluded batch but the
                client never received the original POST response (long
                slice + dropped connection / tab refresh), expose it here
                so the polling UI can still show the dialog."""
                try:
                    candidates = [
                        (eid, info)
                        for eid, info in self.temp_stl_files.items()
                        if info.get('is_excluded_batch')
                    ]
                except Exception:
                    return None
                if not candidates:
                    return None
                # Newest first by upload_time
                candidates.sort(key=lambda x: x[1].get('upload_time', 0), reverse=True)
                eid, info = candidates[0]
                names = info.get('filenames') or []
                count = len(names) if names else len(info.get('paths') or [])
                if count <= 0:
                    return None
                batch_num = info.get('batch_num', 2)
                payload = {
                    'excluded_id': eid,
                    'excluded_models': {
                        'count': count,
                        'names': names,
                        'message': (
                            f'{count} model{"s were" if count > 1 else " was"} '
                            f'excluded in this print. Do you want to slice '
                            f'{"them" if count > 1 else "it"} for another print?'
                        ),
                    },
                    'batch_num': batch_num,
                }
                timed_out = info.get('timed_out_names') or []
                if timed_out:
                    payload['timed_out_models'] = {
                        'count': len(timed_out),
                        'names': timed_out,
                    }
                return payload

            def _pending_timed_out_only_payload():
                """A slice that produced a normal output but had heavy
                meshes time out. Carries the same friendly hint via
                polling so the message survives a dropped POST."""
                try:
                    candidates = [
                        (tid, info)
                        for tid, info in self.temp_stl_files.items()
                        if info.get('is_timed_out_only')
                    ]
                except Exception:
                    return None
                if not candidates:
                    return None
                candidates.sort(key=lambda x: x[1].get('upload_time', 0), reverse=True)
                tid, info = candidates[0]
                names = info.get('timed_out_names') or []
                if not names:
                    return None
                return {
                    'timed_out_id': tid,
                    'timed_out_models': {
                        'count': len(names),
                        'names': names,
                    },
                }

            try:
                # Check if there's active slicing
                queue_status = self.auto_slicer.get_queue_status()

                if not queue_status['active_operations']:
                    payload = {
                        'status': 'idle',
                        'stage': None,
                        'current_file': 0,
                        'total_files': 0
                    }
                    pending = _pending_excluded_payload()
                    if pending:
                        payload['pending_excluded'] = pending
                    else:
                        timed_out_only = _pending_timed_out_only_payload()
                        if timed_out_only:
                            payload['pending_timed_out'] = timed_out_only
                    return jsonify(payload)
                
                # Get the active operation
                active_op = queue_status['active_operations'][0]
                
                # Check if we have detailed progress info
                if hasattr(self.auto_slicer, 'slicing_progress'):
                    progress = self.auto_slicer.slicing_progress
                    payload = {
                        'status': 'active',
                        'stage': progress.get('stage', 'slicing'),
                        'current_file': progress.get('current_file', 1),
                        'total_files': progress.get('total_files', 1),
                        'material': active_op.get('material', '')
                    }
                    # Smart Engine slices report REAL overall progress 0..1
                    if isinstance(progress.get('progress'), (int, float)):
                        payload['progress'] = float(progress['progress'])
                    return jsonify(payload)
                else:
                    # Fallback to basic info
                    total_files = 1
                    if 'STL files' in active_op.get('filename', ''):
                        import re
                        match = re.search(r'(\d+) STL files', active_op['filename'])
                        if match:
                            total_files = int(match.group(1))
                    
                    return jsonify({
                        'status': 'active',
                        'stage': 'slicing',
                        'current_file': 1,
                        'total_files': total_files,
                        'material': active_op.get('material', '')
                    })
                    
            except Exception as e:
                logger.error(f"Error in /api/slicing-progress: {str(e)}")
                return jsonify({
                    'status': 'idle',
                    'stage': None,
                    'current_file': 0,
                    'total_files': 0
                })

        @self.app.route('/api/upload', methods=['POST'])
        def upload_file():
            try:
                logger.info("Upload route called")
                
                if 'file' not in request.files:
                    logger.error("No file part in request")
                    return jsonify({'success': False, 'message': 'No file part'}), 400
                    
                file = request.files['file']
                logger.info(f"File received: {file.filename}")
                
                if file.filename == '':
                    logger.error("No file selected")
                    return jsonify({'success': False, 'message': 'No selected file'}), 400
                    
                filename_lower = file.filename.lower()
                if file and (filename_lower.endswith('.zip') or filename_lower.endswith('.stl')):
                    filename = secure_filename(file.filename)
                    logger.info(f"Processing file: {filename}")

                    try:
                        if filename_lower.endswith('.zip'):
                            logger.info("Processing ZIP file")
                            # Handle ZIP files - upload and extract as before.
                            # If a file with the same name already exists in
                            # Uploads/, suffix the new one with _2/_3/... so
                            # the existing copy is preserved.
                            filename = self._unique_upload_filename(filename)
                            file_path = os.path.join(self.uploads_path, filename)
                            file.save(file_path)
                            logger.info(f"ZIP file uploaded: {filename}")
                            
                            self._cleanup_old_files()
                            self.find_zip_files()
                            
                            try:
                                logger.info(f"Auto-extracting uploaded ZIP file: {filename}")
                                self.extract_zip_file(file_path)
                                
                                return jsonify({
                                    'success': True, 
                                    'message': f'ZIP file {filename} uploaded and extracted successfully',
                                    'auto_extracted': True,
                                    'filename': filename
                                })
                            except Exception as extract_error:
                                logger.error(f"Error auto-extracting file: {str(extract_error)}")
                                return jsonify({
                                    'success': True, 
                                    'message': f'ZIP file {filename} uploaded but extraction failed: {str(extract_error)}',
                                    'auto_extracted': False,
                                    'filename': filename
                                })
                        
                        else:
                            logger.info("Processing STL file")
                            # Handle STL files - return material selection required
                            logger.info(f"STL file received: {filename}")
                            
                            # A bundle is only needed for the LEGACY slicer
                            # path — the in-process Smart Engine slices with
                            # its own resin profiles, so don't dead-end the
                            # upload when the engine can take it.
                            if not self.auto_slicer.current_bundle and not (
                                    engine_bridge is not None
                                    and engine_bridge.available()):
                                logger.error("No bundle selected")
                                return jsonify({
                                    'success': False,
                                    'message': 'No bundle selected. Please select a bundle in Settings first.',
                                    'is_stl': True
                                })
                            
                            # Save STL temporarily and return material selection requirement
                            import tempfile
                            logger.info("Creating temporary file for STL")
                            
                            try:
                                with tempfile.NamedTemporaryFile(suffix='.stl', delete=False) as temp_file:
                                    file.save(temp_file.name)
                                    temp_stl_path = temp_file.name
                                logger.info(f"STL saved to temporary path: {temp_stl_path}")
                            except Exception as temp_error:
                                logger.error(f"Error creating temporary file: {str(temp_error)}")
                                return jsonify({'success': False, 'message': f'Error creating temporary file: {str(temp_error)}'}), 500
                            
                            # Store the temp path for later use
                            temp_stl_id = str(int(time.time() * 1000))  # Use timestamp as ID
                            self.temp_stl_files[temp_stl_id] = {
                                'path': temp_stl_path,
                                'filename': filename,
                                'upload_time': time.time()
                            }
                            
                            # Add to queue immediately for UI display
                            self.auto_slicer.queued_operations.append({
                                'id': temp_stl_id,
                                'type': 'single',
                                'filename': filename,
                                'material': 'Awaiting material selection'
                            })
                            
                            logger.info(f"STL stored with ID: {temp_stl_id}")
                            
                            return jsonify({
                                'success': True,
                                'message': 'STL file uploaded, select material to slice',
                                'requires_material_selection': True,
                                'temp_stl_id': temp_stl_id,
                                'filename': filename,
                                'is_stl': True
                            })
                                
                    except Exception as e:
                        logger.error(f"Error processing uploaded file: {str(e)}")
                        import traceback
                        logger.error(f"Traceback: {traceback.format_exc()}")
                        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500
                else:
                    logger.error(f"Invalid file type: {filename_lower}")
                    return jsonify({'success': False, 'message': 'Only .zip and .stl files are allowed'}), 400
                    
            except Exception as e:
                logger.error(f"Unexpected error in upload route: {str(e)}")
                import traceback
                logger.error(f"Traceback: {traceback.format_exc()}")
                return jsonify({'success': False, 'message': f'Server error: {str(e)}'}), 500

        @self.app.route('/api/cleanup-temp-stl', methods=['POST'])
        def cleanup_temp_stl():
            try:
                data = request.json
                temp_stl_id = data.get('temp_stl_id')
                
                if not temp_stl_id:
                    return jsonify({'success': False, 'message': 'No temp_stl_id provided'}), 400
                
                # Remove from temp files
                if temp_stl_id in self.temp_stl_files:
                    temp_stl_info = self.temp_stl_files[temp_stl_id]
                    
                    # Clean up temporary files
                    if temp_stl_info.get('is_multiple', False):
                        for temp_path in temp_stl_info['paths']:
                            try:
                                os.unlink(temp_path)
                            except:
                                pass
                    else:
                        try:
                            os.unlink(temp_stl_info['path'])
                        except:
                            pass
                    
                    del self.temp_stl_files[temp_stl_id]
                
                # Remove from queue
                self.auto_slicer.queued_operations = [
                    op for op in self.auto_slicer.queued_operations if op['id'] != temp_stl_id
                ]
                
                return jsonify({'success': True, 'message': 'Temp STL cleaned up successfully'})
                
            except Exception as e:
                logger.error(f"Error in cleanup-temp-stl route: {str(e)}")
                return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500
                    
        @self.app.route('/api/slice-with-material', methods=['POST'])
        def slice_with_material():
            """Slice STL with specified material"""
            guard = self._require_screen()
            if guard:
                return guard
            try:
                data = request.json
                temp_stl_id = data.get('temp_stl_id')
                material_name = data.get('material_name')

                if not temp_stl_id:
                    return jsonify({'success': False, 'message': 'Missing temp_stl_id'}), 400

                # Merged deploy: the Smart Engine auto-slices in-process (Abu
                # Layer orientation + supports, panel-native resolution,
                # one-family batches straight into the queue). material_name
                # may be empty here — the dialog's MATIC Smart Engine option
                # then slices each family batch with its part type's default
                # resin from the Material classifier (engine default when a
                # family has none). The legacy external-slicer path below
                # survives only for standalone firmware builds.
                if engine_bridge is not None and engine_bridge.available():
                    payload, status = engine_bridge.control_slice_with_material(
                        self, data)
                    return jsonify(payload), status

                if not material_name:
                    return jsonify({'success': False, 'message': 'Missing material_name'}), 400
                
                if temp_stl_id not in self.temp_stl_files:
                    return jsonify({'success': False, 'message': 'STL file not found or expired'}), 400
                
                temp_stl_info = self.temp_stl_files[temp_stl_id]
                
                # Update queue item with selected material
                for operation in self.auto_slicer.queued_operations:
                    if operation['id'] == temp_stl_id:
                        operation['material'] = material_name
                        break
                
                # Check if this is multiple STL files or single
                if temp_stl_info.get('is_multiple', False):
                    # Multiple STL files
                    temp_stl_paths = temp_stl_info['paths']
                    original_filenames = temp_stl_info.get('filenames', [])
                    
                    logger.info(f"Processing {len(temp_stl_paths)} STL files with material {material_name}")
                    logger.info(f"Original filenames: {original_filenames}")
                    
                    # Let slice_multiple_stl_with_material generate the name automatically
                    output_name = None
                    
                    # Call the multi-STL slicing method with original filenames
                    success, result, excluded_info = self.auto_slicer.slice_multiple_stl_with_material(
                        temp_stl_paths, 
                        output_name, 
                        material_name,
                        original_stl_names=original_filenames  # Pass original filenames
                    )
                    
                    # Clean up temporary files for successfully placed models
                    # Note: Don't delete excluded model files yet
                    for temp_path in temp_stl_paths:
                        if excluded_info:
                            # Only delete if not in excluded paths
                            if temp_path not in excluded_info.get('paths', []):
                                try:
                                    os.unlink(temp_path)
                                except:
                                    pass
                        else:
                            # No excluded models, delete all temp files
                            try:
                                os.unlink(temp_path)
                            except:
                                pass
                    
                    del self.temp_stl_files[temp_stl_id]
                    
                    # Remove from queued operations since slice method will handle it
                    self.auto_slicer.queued_operations = [
                        op for op in self.auto_slicer.queued_operations if op['id'] != temp_stl_id
                    ]
                    
                    if success:
                        # Clean up old files after successful slice
                        self._cleanup_old_files()
                        
                        # Refresh file list
                        self.find_zip_files()
                        
                        response_data = {
                            'success': True,
                            'message': f'{len(temp_stl_paths) - (excluded_info["count"] if excluded_info else 0)} of {len(temp_stl_paths)} STL files sliced with {material_name} material',
                            'filename': result,
                            'auto_sliced': True
                        }
                        
                        # CRITICAL FIX: Add excluded models info if any were excluded
                        if excluded_info and excluded_info['count'] > 0:
                            logger.info(f"Excluded models detected: {excluded_info['count']} models")
                            logger.info(f"Excluded names: {excluded_info.get('names', [])}")

                            # Store excluded info for follow-up dialog
                            excluded_id = f"excluded_{int(time.time() * 1000)}"
                            self.temp_stl_files[excluded_id] = {
                                'paths': excluded_info['paths'],
                                'filenames': excluded_info.get('names', []),  # Use the actual STL names
                                'material': excluded_info['material'],
                                'is_multiple': True,
                                'is_excluded_batch': True,
                                'batch_num': 2,  # Start at batch 2
                                'upload_time': time.time(),
                                'timed_out_names': excluded_info.get('timed_out_names', []),
                            }

                            response_data['excluded_models'] = {
                                'count': excluded_info['count'],
                                'names': excluded_info.get('names', []),  # Show actual STL names
                                'message': f'{excluded_info["count"]} model{"s were" if excluded_info["count"] > 1 else " was"} excluded in this print. Do you want to slice {"them" if excluded_info["count"] > 1 else "it"} for another print?'
                            }
                            response_data['excluded_id'] = excluded_id

                            logger.info(f"Sending excluded models dialog info to frontend: {response_data['excluded_models']}")

                        # Surface timed-out STLs separately. Carried alongside
                        # the success response so the user sees a friendly
                        # "please slice this in PrusaSlicer on your computer"
                        # message — not an error, just a workflow hint.
                        timed_out = (excluded_info or {}).get('timed_out_names') or []
                        if timed_out:
                            logger.info(f"Timed-out STLs surfaced to UI: {timed_out}")
                            response_data['timed_out_models'] = {
                                'count': len(timed_out),
                                'names': timed_out,
                            }
                            # If there's no excluded batch, still stash the
                            # timed-out info so the polling recovery path can
                            # show it after a dropped POST.
                            if 'excluded_id' not in response_data:
                                pending_id = f"timedout_{int(time.time() * 1000)}"
                                self.temp_stl_files[pending_id] = {
                                    'is_excluded_batch': False,
                                    'is_timed_out_only': True,
                                    'timed_out_names': timed_out,
                                    'upload_time': time.time(),
                                }
                                response_data['timed_out_id'] = pending_id

                        return jsonify(response_data)
                    else:
                        logger.error(f"Slicing failed: {result}")
                        return jsonify({'success': False, 'message': result}), 500
                        
                else:
                    # Single STL file handling
                    temp_stl_path = temp_stl_info['path']
                    original_filename = temp_stl_info['filename']
                    
                    logger.info(f"Slicing single STL {original_filename} with material {material_name}")
                    
                    # Generate output name for single file - use same format as multi-STL
                    # Let slice_stl_with_material generate the name automatically
                    output_name = None
                    
                    # Call single STL slicing method
                    success, result = self.auto_slicer.slice_stl_with_material(
                        temp_stl_path, 
                        output_name, 
                        material_name,
                        original_filename=original_filename
                    )
                    
                    # Clean up temp file
                    try:
                        os.unlink(temp_stl_path)
                    except:
                        pass
                    del self.temp_stl_files[temp_stl_id]
                    
                    # Remove from queued operations
                    self.auto_slicer.queued_operations = [
                        op for op in self.auto_slicer.queued_operations if op['id'] != temp_stl_id
                    ]
                    
                    if success:
                        # Clean up old files after successful slice
                        self._cleanup_old_files()
                        
                        # Refresh file list
                        self.find_zip_files()
                        
                        # IMPORTANT: Clear the queue after successful slicing
                        self.auto_slicer.queued_operations = []
                        self.auto_slicer.active_slicing_operations = {}
                        
                        return jsonify({
                            'success': True,
                            'message': f'STL file sliced successfully with {material_name} material',
                            'filename': result,
                            'auto_sliced': True
                        })
                    else:
                        return jsonify({'success': False, 'message': result}), 500                        
            except Exception as e:
                logger.error(f"Error in slice-with-material route: {str(e)}")
                import traceback
                logger.error(f"Traceback: {traceback.format_exc()}")
                return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500
                                
        @self.app.route('/api/dismiss-timed-out', methods=['POST'])
        def dismiss_timed_out():
            """Drop a pending timed-out-only notification so the polling
            recovery path stops surfacing it. Called when the user
            acknowledges the 'please slice on workstation' hint."""
            try:
                data = request.json or {}
                timed_out_id = data.get('timed_out_id')
                if not timed_out_id:
                    return jsonify({'success': False, 'message': 'Missing timed_out_id'}), 400
                info = self.temp_stl_files.get(timed_out_id)
                if info and info.get('is_timed_out_only'):
                    del self.temp_stl_files[timed_out_id]
                return jsonify({'success': True})
            except Exception as e:
                logger.error(f"Error in dismiss-timed-out: {str(e)}")
                return jsonify({'success': False, 'message': str(e)}), 500

        @self.app.route('/api/slice-excluded-models', methods=['POST'])
        def slice_excluded_models():
            """Handle slicing of excluded models from a previous batch"""
            guard = self._require_screen()
            if guard:
                return guard
            try:
                data = request.json
                excluded_id = data.get('excluded_id')
                action = data.get('action')  # 'slice', 'cancel', or 'details'
                
                if not excluded_id or excluded_id not in self.temp_stl_files:
                    return jsonify({'success': False, 'message': 'Excluded models data not found or expired'}), 400
                
                excluded_info = self.temp_stl_files[excluded_id]
                
                if action == 'details':
                    # Return details about excluded models
                    return jsonify({
                        'success': True,
                        'details': {
                            'count': len(excluded_info['filenames']),
                            'names': excluded_info['filenames'],
                            'material': excluded_info['material']
                        }
                    })
                
                elif action == 'slice':
                    # Slice the excluded models
                    temp_stl_paths = excluded_info['paths']
                    material_name = excluded_info['material']
                    original_names = excluded_info.get('filenames', [])
                    batch_num = excluded_info.get('batch_num', 2)
                    
                    logger.info(f"Processing {len(temp_stl_paths)} excluded models with material {material_name} (Batch {batch_num})")
                    logger.info(f"Model names: {original_names}")
                    logger.info(f"Model paths: {temp_stl_paths}")
                    
                    # Verify the paths exist
                    for path in temp_stl_paths:
                        if not os.path.exists(path):
                            logger.error(f"Path does not exist: {path}")
                        else:
                            logger.info(f"Path exists: {path} (size: {os.path.getsize(path)} bytes)")
                    
                    # IMPORTANT: DON'T add to queue for excluded batches - they're handled via dialog
                    # The progress will be shown in the slicing dialog, not the queue
                    
                    # Handle recursive slicing with dialog support
                    all_outputs, last_excluded_info = self.auto_slicer.handle_excluded_models_recursive(
                        temp_stl_paths, 
                        material_name,
                        original_names,
                        batch_num
                    )
                    
                    # Clean up current excluded info
                    del self.temp_stl_files[excluded_id]
                    
                    # IMPORTANT: Clean up any stale queue items
                    self.auto_slicer.queued_operations = []
                    self.auto_slicer.active_slicing_operations = {}
                    
                    if all_outputs:
                        logger.info(f"Successfully created {len(all_outputs)} output files")
                        
                        # Clean up old files and refresh
                        self._cleanup_old_files()
                        self.find_zip_files()
                        
                        # Determine final message based on whether all models were processed
                        if last_excluded_info and last_excluded_info['count'] > 0:
                            # Still have excluded models - shouldn't happen if loop works correctly
                            logger.warning(f"Still have {last_excluded_info['count']} excluded models after all batches")
                            message = f'Successfully sliced batches 2-{batch_num}. Warning: {last_excluded_info["count"]} models could not be placed.'
                        else:
                            # All models processed successfully
                            final_batch = batch_num + len(all_outputs) - 1
                            if len(all_outputs) == 1:
                                message = f'Successfully sliced Batch {batch_num}'
                            else:
                                message = f'Successfully sliced all remaining models in {len(all_outputs)} batches (Batch {batch_num} through Batch {final_batch})'
                        
                        response_data = {
                            'success': True,
                            'filenames': all_outputs,
                            'auto_sliced': True,
                            'message': message
                        }
                        
                        # No more excluded_models dialog - we processed everything
                        logger.info(f"All models processed successfully after batch {batch_num}")
                        
                        return jsonify(response_data)
                    else:
                        logger.error(f"No outputs created for batch {batch_num}")
                        # Clean up queue on failure too
                        self.auto_slicer.queued_operations = []
                        self.auto_slicer.active_slicing_operations = {}
                        return jsonify({
                            'success': False,
                            'message': 'Failed to slice excluded models - no outputs created'
                        }), 500
                
                elif action == 'cancel':
                    # User chose not to slice excluded models
                    # Clean up temp files
                    temp_stl_paths = excluded_info['paths']
                    for temp_path in temp_stl_paths:
                        try:
                            os.unlink(temp_path)
                            logger.info(f"Deleted temp file: {temp_path}")
                        except Exception as e:
                            logger.error(f"Failed to delete {temp_path}: {str(e)}")
                    
                    del self.temp_stl_files[excluded_id]
                    
                    # Clean up any stale queue items when canceling
                    self.auto_slicer.queued_operations = []
                    self.auto_slicer.active_slicing_operations = {}
                    
                    return jsonify({
                        'success': True,
                        'message': 'Excluded models discarded'
                    })
                
                else:
                    return jsonify({'success': False, 'message': 'Invalid action'}), 400
                    
            except Exception as e:
                logger.error(f"Error in slice-excluded-models endpoint: {str(e)}")
                import traceback
                logger.error(f"Traceback: {traceback.format_exc()}")
                # Clean up queue on error
                self.auto_slicer.queued_operations = []
                self.auto_slicer.active_slicing_operations = {}
                return jsonify({'success': False, 'message': str(e)}), 500
                                            
        @self.app.route('/api/queue-status')
        def get_queue_status():
            try:
                queue_status = self.auto_slicer.get_queue_status()
                return jsonify(queue_status)
            except Exception as e:
                logger.error(f"Error in /api/queue-status route: {str(e)}")
                return jsonify({'active_operations': [], 'queued_operations': [], 'queue_length': 0})
            
        # BUNDLE MANAGEMENT ROUTES
        @self.app.route('/api/bundles')
        def get_bundles():
            try:
                bundles = self.auto_slicer.get_bundle_files()
                return jsonify(bundles)
            except Exception as e:
                logger.error(f"Error in /api/bundles route: {str(e)}")
                return jsonify({'error': str(e)}), 500
                
        @self.app.route('/api/bundles/upload', methods=['POST'])
        def upload_bundle():
            # Check if slicing is active
            if self.is_slicing_active():
                return jsonify({
                    'success': False, 
                    'message': 'Cannot upload bundle while slicing is in progress'
                }), 400
            
            if 'file' not in request.files:
                return jsonify({'success': False, 'message': 'No file part'}), 400
                
            file = request.files['file']
            
            if file.filename == '':
                return jsonify({'success': False, 'message': 'No selected file'}), 400
            
            success, message = self.auto_slicer.upload_bundle(file)
            return jsonify({'success': success, 'message': message})

        @self.app.route('/api/bundles/select', methods=['POST'])
        def select_bundle():
            # Check if slicing is active
            if self.is_slicing_active():
                return jsonify({
                    'success': False, 
                    'message': 'Cannot change bundle while slicing is in progress'
                }), 400
            
            data = request.json
            filename = data.get('filename')
            
            if not filename:
                return jsonify({'success': False, 'message': 'No filename provided'}), 400
            
            try:
                result = self.auto_slicer.select_bundle(filename)
                
                if result is None:
                    logger.error(f"select_bundle returned None for filename: {filename}")
                    return jsonify({'success': False, 'message': 'Bundle selection failed - internal error'})
                
                success, message = result
                return jsonify({'success': success, 'message': message})
                
            except Exception as e:
                logger.error(f"Exception in select_bundle route: {str(e)}")
                return jsonify({'success': False, 'message': f'Bundle selection failed: {str(e)}'})

        @self.app.route('/api/bundles/delete', methods=['POST'])
        def delete_bundle():
            # Check if slicing is active
            if self.is_slicing_active():
                return jsonify({
                    'success': False, 
                    'message': 'Cannot delete bundle while slicing is in progress'
                }), 400
            
            data = request.json
            filename = data.get('filename')
            
            if not filename:
                return jsonify({'success': False, 'message': 'No filename provided'}), 400
            
            success, message = self.auto_slicer.delete_bundle(filename)
            return jsonify({'success': success, 'message': message})
        
        @self.app.route('/api/bundles/current')
        def get_current_bundle():
            try:
                current_bundle = self.auto_slicer.get_current_bundle_basename()
                return jsonify({'current_bundle': current_bundle})
            except Exception as e:
                logger.error(f"Error getting current bundle: {str(e)}")
                return jsonify({'current_bundle': 'Error loading bundle info'})
        
        @self.app.route('/api/delete', methods=['POST'])
        def delete_file():
            data = request.json
            filename = data.get('filename')
            
            if not filename:
                return jsonify({'success': False, 'message': 'No filename provided'}), 400
                
            # Find the full path of the file
            file_path = None
            for file in self.zip_files:
                if file['name'] == filename:
                    file_path = file['path']
                    break
            
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    logger.info(f"File deleted: {filename}")
                    self.find_zip_files()  # Refresh file list
                    return jsonify({'success': True, 'message': f'File {filename} deleted'})
                except Exception as e:
                    logger.error(f"Error deleting file: {str(e)}")
                    return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500
            else:
                return jsonify({'success': False, 'message': 'File not found'}), 404
                
        @self.app.route('/api/extract', methods=['POST'])
        def extract_file():
            data = request.json
            filename = data.get('filename')
            
            # Only extract ZIP files
            if not filename or not filename.lower().endswith('.zip'):
                return jsonify({'success': False, 'message': 'Only ZIP files can be extracted'})
            
            # Check printing/movement status before attempting extraction
            if self.is_printing:
                return jsonify({
                    'success': False, 
                    'message': 'Cannot extract files while printing is in progress'
                })
            
            if self.is_moving:
                return jsonify({
                    'success': False, 
                    'message': 'Cannot extract files while platform is moving'
                })
            
            # Find the full path of the file
            file_path = None
            for file in self.zip_files:
                if file['name'] == filename:
                    file_path = file['path']
                    break
            
            if file_path:
                try:
                    self.extract_zip_file(file_path)
                    return jsonify({'success': True, 'message': f'Extracted {filename}'})
                except Exception as e:
                    error_msg = str(e)
                    if "printing is in progress" in error_msg:
                        return jsonify({
                            'success': False, 
                            'message': 'Cannot extract files while printing is in progress'
                        })
                    elif "platform is moving" in error_msg:
                        return jsonify({
                            'success': False, 
                            'message': 'Cannot extract files while platform is moving'
                        })
                    else:
                        return jsonify({'success': False, 'message': f'Error: {error_msg}'})
            else:
                return jsonify({'success': False, 'message': 'File not found'})
                                            
        @self.app.route('/api/status')
        def get_status():
            # Get printer status
            with self.lock:
                position_tolerance = 1.0
                at_home_position = abs(self.current_position_mm - HOME_POSITION) <= position_tolerance
                at_zero_position = abs(self.current_position_mm - 0.0) <= position_tolerance
                can_start_print = (
                    not self.is_printing and 
                    not self.is_moving and 
                    not self.is_stopping and
                    not self.is_cleaning and  # NEW: Cannot start print during cleaning
                    self.motor_enabled and 
                    (at_home_position or at_zero_position)
                )
                
                # Determine why printing cannot be started (if applicable)
                start_print_status = ""
                if self.is_printing:
                    start_print_status = "Print in progress"
                elif self.is_stopping:
                    start_print_status = "Stopping print in progress"
                elif self.is_cleaning:  # NEW: Check cleaning state
                    start_print_status = "Cleaning in progress"
                elif self.is_moving:
                    start_print_status = "Motor is moving"
                elif not self.motor_enabled:
                    start_print_status = "Motor is disabled"
                elif not (at_home_position or at_zero_position):
                    start_print_status = f"Platform not at Home or Zero position ({abs(self.current_position_mm):.1f}mm)"
                else:
                    start_print_status = "Ready to print"
                
                # Calculate heating progress
                heating_progress = 0
                heating_remaining_time = 0
                if self.is_heating and hasattr(self, 'heating_start_time'):
                    elapsed_time = time.time() - self.heating_start_time
                    heating_progress = min(100, (elapsed_time / self.heating_time) * 100)
                    heating_remaining_time = max(0, self.heating_time - elapsed_time)

                # Get current temperature
                temp = self.get_temperature()
                temp_status = self.get_temperature_status()
                
                status = {
                    'is_printing': self.is_printing,
                    'is_moving': self.is_moving,
                    'is_stopping': self.is_stopping,
                    'is_paused': self.is_paused,
                    'pause_processing': self.pause_processing,
                    'is_cleaning': self.is_cleaning,  # NEW: Include cleaning state
                    'current_layer': self.current_layer,
                    'total_layers': self.actual_slice_count,
                    'position': f"{abs(self.current_position_mm):.2f}",
                    'motor_enabled': self.motor_enabled,
                    'message': self.status_message,
                    'estimated_print_time': self.estimated_print_time,
                    'print_start_time': self.print_start_time if hasattr(self, 'print_start_time') else None,
                    'current_layer_white_ratio': self.current_layer_white_ratio,
                    'next_layer_white_ratio': self.next_layer_white_ratio if self.is_printing else 0,
                    'can_start_print': can_start_print,
                    'start_print_status': start_print_status,
                    'at_home_position': at_home_position,
                    'at_zero_position': at_zero_position,
                    'is_heating': self.is_heating,
                    'heating_time': self.heating_time,
                    'heating_progress': heating_progress,          
                    'heating_remaining_time': heating_remaining_time,
                    'is_design_slicing': bool(self.is_design_slicing),
                    'design_slice_phase': self.design_slice_phase or 'idle',
                    'design_slicing_label': self.design_slicing_label or '',
                    'design_slice_file_count': int(self.design_slice_file_count or 0),
                    'design_slice_material': self.design_slice_material or '',
                    'design_slice_patient': self.design_slice_patient or '',
                    'is_curing': self.cure.is_curing if self.cure else False,
                    'cure_progress': round(self.cure.cure_progress, 1) if self.cure else 0,
                    'cure_material': self.cure.current_material if self.cure else None,
                    'cure_status': self.cure.cure_status if self.cure else 'Unavailable',
                    'cure_phase': self.cure.cure_phase if self.cure else 'idle',
                    'firmware_version': FIRMWARE_VERSION,
                    'firmware_update': FIRMWARE_UPDATE,
                    'build_number': BUILD_NUMBER,
                    'build_date': BUILD_DATE,
                    'device_serial': self.device_serial,
                    'temperature': round(temp, 1),
                    'temperature_status': temp_status,
                    'temp_warning_threshold': self.temp_warning_threshold,
                    'temp_critical_threshold': self.temp_critical_threshold,
                    'screen_connected': self.screen_connected
                    }
            return jsonify(status)
        
        @self.app.route('/api/system/safe-shutdown', methods=['POST'])
        def safe_shutdown():
            """Gracefully shutdown the system after syncing filesystem"""
            try:
                if self.is_printing or self.is_moving or self.is_stopping:
                    return jsonify({
                        'success': False, 
                        'message': 'Cannot shutdown while printing, moving, or stopping. Please wait for operations to complete.'
                    }), 400
                
                logger.info("Safe shutdown initiated")
                self.status_message = "Shutting down safely..."
                
                # Log the shutdown event
                logger.info("System shutting down - syncing filesystem...")
                
                # Sync filesystem to ensure all writes are committed
                subprocess.run(['sync'], check=False, timeout=5)
                logger.info("Filesystem synced")
                
                # Schedule shutdown in a background thread to allow response to be sent
                def delayed_shutdown():
                    time.sleep(1)
                    try:
                        subprocess.run(['sudo', 'shutdown', 'now'], check=False, timeout=10)
                    except Exception as e:
                        logger.error(f"Shutdown command failed: {e}")
                
                threading.Thread(target=delayed_shutdown, daemon=True).start()
                return jsonify({'success': True, 'message': 'System shutting down safely. Please wait...'}), 200
            except Exception as e:
                logger.error(f"Safe shutdown failed: {e}")
                return jsonify({'success': False, 'message': f'Shutdown error: {str(e)}'})
        
        @self.app.route('/api/print/start', methods=['POST'])
        def start_print():
            # No LCD panel -> nothing to expose layers onto. Refuse cleanly.
            guard = self._require_screen()
            if guard:
                return guard
            # Block print starts while a Design Portal slice is in flight.
            # Starting a print would extract whatever is currently selected
            # but the UI auto-selects the latest sliced file -- if the
            # slicer is still writing, the user could race it.
            if self.is_design_slicing and not self.is_paused:
                return jsonify({
                    'success': False,
                    'message': 'Slicing a Design Portal job -- wait until done.',
                })
            # A zip is mid-extraction into Sliced/: starting now would print
            # a half-cleared / half-written layer set.
            if self.is_extracting:
                return jsonify({
                    'success': False,
                    'message': 'Preparing the selected file -- try again in a moment.',
                })
            if not self.is_printing:
                # Load parameters first to ensure we have them
                if self.load_print_parameters():
                    # Start print in a thread to not block the web request
                    threading.Thread(target=self.start_print).start()
                    return jsonify({'success': True, 'message': 'Print started'})
                else:
                    return jsonify({'success': False, 'message': 'Failed to load print parameters'})
            elif self.is_paused:
                # Resume print
                self.resume_print()
                return jsonify({'success': True, 'message': 'Print resumed'})
            else:
                return jsonify({'success': False, 'message': 'Printing already in progress'})
        
        @self.app.route('/api/print/pause', methods=['POST'])
        def pause_print():
            if self.is_printing and not self.is_paused:
                self.pause_print()
                return jsonify({'success': True, 'message': 'Pausing print after current layer'})
            else:
                return jsonify({'success': False, 'message': 'No print in progress or already paused'})
        
        @self.app.route('/api/print/stop', methods=['POST'])
        def stop_print():
            if self.is_printing:
                self.stop_print()
                return jsonify({'success': True, 'message': 'Print stopped'})
            else:
                return jsonify({'success': False, 'message': 'No print in progress'})
        
        
        @self.app.route('/api/motor/toggle', methods=['POST'])
        def toggle_motor():
            self.toggle_motor()
            return jsonify({'success': True, 'message': f'Motor {"disabled" if not self.motor_enabled else "enabled"}'})
        
        @self.app.route('/api/move/down', methods=['POST'])
        def move_down():
            try:
                self.move_down_100mm()
                return jsonify({'success': True, 'message': 'Platform moved down'})
            except Exception as e:
                return jsonify({'success': False, 'message': f'Error: {str(e)}'})
        
        @self.app.route('/api/exit', methods=['POST'])
        def exit_program():
            # Spawn a thread to exit the program after sending response
            threading.Thread(target=self.exit_program).start()
            return jsonify({'success': True, 'message': 'Exiting program'})

        # ---- Wi-Fi management (nmcli on the host) ----
        # Uses sudo because the service runs as user MATIC (which has NOPASSWD sudo)
        # and polkit does not grant NetworkManager.modify.system to background processes.
        def _nmcli(args, timeout=10, sudo=False):
            cmd = (['sudo', '-n'] if sudo else []) + ['nmcli'] + args
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

        def _parse_terse(line):
            # nmcli -t uses ':' separators and escapes literal ':' and '\' with '\'
            fields, buf, i = [], [], 0
            while i < len(line):
                c = line[i]
                if c == '\\' and i + 1 < len(line):
                    buf.append(line[i + 1])
                    i += 2
                elif c == ':':
                    fields.append(''.join(buf)); buf = []
                    i += 1
                else:
                    buf.append(c); i += 1
            fields.append(''.join(buf))
            return fields

        @self.app.route('/api/wifi/status', methods=['GET'])
        def wifi_status():
            try:
                r = _nmcli(['-t', '-e', 'yes', '-f', 'NAME,TYPE,DEVICE', 'connection', 'show', '--active'])
                ssid, device = None, None
                for line in r.stdout.splitlines():
                    parts = _parse_terse(line)
                    if len(parts) >= 3 and parts[1] == '802-11-wireless':
                        ssid, device = parts[0], parts[2]
                        break
                ip = None
                if device:
                    r2 = _nmcli(['-t', '-e', 'yes', '-f', 'IP4.ADDRESS', 'device', 'show', device])
                    for line in r2.stdout.splitlines():
                        parts = _parse_terse(line)
                        if len(parts) >= 2 and parts[0].startswith('IP4.ADDRESS') and parts[1]:
                            ip = parts[1].split('/')[0]
                            break
                return jsonify({'success': True, 'connected': ssid is not None, 'ssid': ssid, 'ip': ip})
            except Exception as e:
                logger.warning(f"wifi_status error: {e}")
                return jsonify({'success': False, 'message': str(e)}), 500

        @self.app.route('/api/wifi/scan', methods=['GET', 'POST'])
        def wifi_scan():
            try:
                # `nmcli device wifi rescan` is async — it kicks the scan and
                # returns immediately. NetworkManager also prunes BSSes from
                # its cache after a few minutes of inactivity, leaving just
                # the associated AP — so on POST we always trigger a rescan
                # and wait for results to repopulate.
                force = request.method == 'POST'

                def _list():
                    return _nmcli(['-t', '-e', 'yes', '-f', 'IN-USE,SSID,SIGNAL,SECURITY',
                                   'device', 'wifi', 'list', '--rescan', 'no'], timeout=20)

                if force:
                    # `rescan` is async — kick the scan, then wait for the
                    # radio to finish hopping channels (~5s on Pi 4/5) before
                    # listing. Reliable as long as wifi.powersave=2 is set in
                    # /etc/NetworkManager/conf.d/ — with PM on, the firmware
                    # silently defers scans while associated.
                    _nmcli(['device', 'wifi', 'rescan'], timeout=10)
                    time.sleep(5)
                r = _list()
                if r.returncode != 0:
                    return jsonify({'success': False, 'message': (r.stderr or 'Scan failed').strip()}), 500
                # Merge BSSes: same SSID can appear multiple times (one per AP).
                # Keep the strongest signal and OR any `*` in-use marker.
                by_ssid = {}
                for line in r.stdout.splitlines():
                    parts = _parse_terse(line)
                    if len(parts) < 4:
                        continue
                    in_use, ssid, signal, security = parts[0], parts[1], parts[2], parts[3]
                    if not ssid:
                        continue
                    try:
                        sig = int(signal)
                    except ValueError:
                        sig = 0
                    secured = bool(security) and security != '--'
                    active = in_use.strip() == '*'
                    cur = by_ssid.get(ssid)
                    if cur is None or sig > cur['signal']:
                        by_ssid[ssid] = {
                            'ssid': ssid, 'signal': sig, 'secured': secured,
                            'security': security if secured else '',
                            'active': active or (cur and cur['active']) or False,
                        }
                    elif active:
                        cur['active'] = True
                networks = sorted(by_ssid.values(), key=lambda n: n['signal'], reverse=True)
                return jsonify({'success': True, 'networks': networks})
            except subprocess.TimeoutExpired:
                return jsonify({'success': False, 'message': 'Scan timed out'}), 504
            except Exception as e:
                logger.warning(f"wifi_scan error: {e}")
                return jsonify({'success': False, 'message': str(e)}), 500

        @self.app.route('/api/wifi/connect', methods=['POST'])
        def wifi_connect():
            data = request.get_json(silent=True) or {}
            ssid = (data.get('ssid') or '').strip()
            password = data.get('password') or ''
            if not ssid:
                return jsonify({'success': False, 'message': 'SSID is required'}), 400
            try:
                # If a saved profile with this name already exists, prefer `connection up`
                # so users don't have to re-enter the password.
                saved = _nmcli(['-t', '-f', 'NAME', 'connection', 'show'])
                known = {_parse_terse(l)[0] for l in saved.stdout.splitlines() if l}
                if ssid in known and not password:
                    r = _nmcli(['connection', 'up', 'id', ssid], timeout=45, sudo=True)
                else:
                    args = ['device', 'wifi', 'connect', ssid]
                    if password:
                        args += ['password', password]
                    r = _nmcli(args, timeout=45, sudo=True)
                if r.returncode == 0:
                    return jsonify({'success': True, 'message': f'Connected to {ssid}'})
                err = (r.stderr or r.stdout or 'Connection failed').strip()
                low = err.lower()
                if 'secrets were required' in low or 'no secrets' in low or 'invalid' in low:
                    friendly = 'Incorrect password'
                elif 'not found' in low or 'no network with ssid' in low:
                    friendly = 'Network not in range'
                else:
                    friendly = err.splitlines()[-1][:160]
                return jsonify({'success': False, 'message': friendly}), 400
            except subprocess.TimeoutExpired:
                return jsonify({'success': False, 'message': 'Connection timed out'}), 504
            except Exception as e:
                logger.warning(f"wifi_connect error: {e}")
                return jsonify({'success': False, 'message': str(e)}), 500

        @self.app.route('/api/wifi/forget', methods=['POST'])
        def wifi_forget():
            data = request.get_json(silent=True) or {}
            ssid = (data.get('ssid') or '').strip()
            if not ssid:
                return jsonify({'success': False, 'message': 'SSID is required'}), 400
            try:
                r = _nmcli(['connection', 'delete', 'id', ssid], timeout=15, sudo=True)
                if r.returncode == 0:
                    return jsonify({'success': True, 'message': f'Forgot {ssid}'})
                return jsonify({'success': False, 'message': (r.stderr or 'Delete failed').strip()}), 400
            except Exception as e:
                logger.warning(f"wifi_forget error: {e}")
                return jsonify({'success': False, 'message': str(e)}), 500

        @self.app.route('/api/upload-multiple-stl', methods=['POST'])
        def handle_multiple_stl_upload():
            """Handle multiple STL file uploads for multi-model slicing"""
            try:
                # Get file count from form data
                file_count = request.form.get('file_count', type=int, default=0)
                
                if file_count == 0:
                    return jsonify({'success': False, 'message': 'No files provided'}), 400
                
                temp_stl_paths = []
                original_filenames = []
                
                # Process each file using the correct naming convention
                for i in range(file_count):
                    file_key = f'stl_file_{i}'
                    if file_key in request.files:
                        file = request.files[file_key]
                        if file and file.filename.lower().endswith('.stl'):
                            # Save each STL to a temporary file
                            import tempfile
                            with tempfile.NamedTemporaryFile(suffix='.stl', delete=False) as temp_file:
                                file.save(temp_file.name)
                                temp_stl_paths.append(temp_file.name)
                                original_filenames.append(secure_filename(file.filename))
                                logger.info(f"STL file {i+1} saved: {file.filename}")
                
                if not temp_stl_paths:
                    return jsonify({'success': False, 'message': 'No valid STL files found'}), 400
                
                # A bundle is only needed for the LEGACY slicer path — the
                # in-process Smart Engine slices with its own resin profiles.
                if not self.auto_slicer.current_bundle and not (
                        engine_bridge is not None
                        and engine_bridge.available()):
                    # Clean up temp files
                    for temp_path in temp_stl_paths:
                        try:
                            os.unlink(temp_path)
                        except:
                            pass
                    return jsonify({'success': False, 'message': 'No bundle selected. Please select a bundle in Settings first.'}), 400
                
                # Store temp files for material selection
                temp_stl_id = f"temp_stl_{int(time.time() * 1000)}"
                self.temp_stl_files[temp_stl_id] = {
                    'paths': temp_stl_paths,
                    'filenames': original_filenames,
                    'upload_time': time.time(),
                    'is_multiple': True
                }
                
                # Add to queue immediately for UI display
                self.auto_slicer.queued_operations.append({
                    'id': temp_stl_id,
                    'type': 'multi',
                    'filename': f"{len(temp_stl_paths)} STL files",
                    'material': 'Awaiting material selection'
                })
                
                logger.info(f"Multiple STL files stored with ID: {temp_stl_id}")
                
                return jsonify({
                    'success': True,
                    'message': f'{len(temp_stl_paths)} STL files uploaded, select material to slice',
                    'requires_material_selection': True,
                    'temp_stl_id': temp_stl_id,
                    'filename': f"{len(temp_stl_paths)} STL files",
                    'is_stl': True,
                    'is_multiple': True
                })
                
            except Exception as e:
                logger.error(f"Error in multiple STL upload: {str(e)}")
                import traceback
                logger.error(f"Traceback: {traceback.format_exc()}")
                
                # Clean up any temp files on error
                if 'temp_stl_paths' in locals():
                    for temp_path in temp_stl_paths:
                        try:
                            os.unlink(temp_path)
                        except:
                            pass
                
                return jsonify({'success': False, 'message': f'Error uploading files: {str(e)}'}), 500
            
        logger.info("All routes registered successfully")
        for rule in self.app.url_map.iter_rules():
            logger.info(f"Route registered: {rule.endpoint} -> {rule.rule}")
            
        @self.app.route('/api/heating/start', methods=['POST'])
        def start_heating():
            if self.is_design_slicing:
                return jsonify({
                    'success': False,
                    'message': 'Slicing a Design Portal job -- wait until done.',
                })
            if not self.is_heating and not self.is_cleaning:
                threading.Thread(target=self.heat_resin).start()
                return jsonify({'success': True, 'message': 'Resin heating started'})
            else:
                return jsonify({'success': False, 'message': 'Heating or cleaning already in progress'})
        
        @self.app.route('/api/heating/stop', methods=['POST'])
        def stop_heating_route():
            if self.is_heating:
                self.stop_heating()
                return jsonify({'success': True, 'message': 'Resin heating stopped'})
            else:
                return jsonify({'success': False, 'message': 'No heating in progress'})

        @self.app.route('/api/cleaning/start', methods=['POST'])
        def start_cleaning():
            if not self.is_cleaning and not self.is_heating:
                threading.Thread(target=self.clean_resin).start()
                return jsonify({'success': True, 'message': 'Cleaning started'})
            else:
                return jsonify({'success': False, 'message': 'Cleaning or heating already in progress'})
        
        @self.app.route('/api/cleaning/stop', methods=['POST'])
        def stop_cleaning_route():
            if self.is_cleaning:
                self.stop_cleaning()
                return jsonify({'success': True, 'message': 'Cleaning stopped'})
            else:
                return jsonify({'success': False, 'message': 'No cleaning in progress'})
                    
        @self.app.route('/api/update/upload', methods=['POST'])
        def upload_update():
            try:
                if 'update_file' not in request.files:
                    return jsonify({'success': False, 'message': 'No update file provided'}), 400
                    
                file = request.files['update_file']
                
                # Validate filename
                if not file.filename.endswith('.tar.gz') or 'update' not in file.filename:
                    return jsonify({'success': False, 'message': 'Invalid update file format'}), 400
                
                # Save to temp location
                import tempfile
                temp_dir = tempfile.mkdtemp()
                update_path = os.path.join(temp_dir, secure_filename(file.filename))
                file.save(update_path)
                
                # Generate update ID
                update_id = str(int(time.time()))
                
                # Store update info
                self.pending_updates = getattr(self, 'pending_updates', {})
                self.pending_updates[update_id] = {
                    'path': update_path,
                    'temp_dir': temp_dir,
                    'filename': file.filename,
                    'upload_time': time.time()
                }
                
                return jsonify({'success': True, 'update_id': update_id, 'message': 'Update uploaded successfully'})
                
            except Exception as e:
                logger.error(f"Error uploading update: {str(e)}")
                return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500

        @self.app.route('/api/update/apply', methods=['POST'])
        def apply_update():
            try:
                data = request.json
                update_id = data.get('update_id')
                
                if not hasattr(self, 'pending_updates') or update_id not in self.pending_updates:
                    return jsonify({'success': False, 'message': 'Update not found'}), 404
                
                update_info = self.pending_updates[update_id]
                
                # Extract and run update in background
                threading.Thread(target=self._apply_update_background, args=(update_info,)).start()
                
                return jsonify({'success': True, 'message': 'Update process started'})
                
            except Exception as e:
                logger.error(f"Error applying update: {str(e)}")
                return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500

        def _apply_update_background(self, update_info):
            """Apply update in background"""
            try:
                import tarfile
                
                # Extract update
                with tarfile.open(update_info['path'], 'r:gz') as tar:
                    tar.extractall(update_info['temp_dir'])
                
                # Run update handler
                update_script = os.path.join(update_info['temp_dir'], 'update_handler.py')
                if os.path.exists(update_script):
                    subprocess.run(['sudo', 'python3', update_script], cwd=update_info['temp_dir'])
                
                # Cleanup
                shutil.rmtree(update_info['temp_dir'])
                
            except Exception as e:
                logger.error(f"Error in background update: {str(e)}")
                
    def _ring_protected_name(self):
        """The armed/printing batch's queue zip name (Sliced/.current_zip) —
        exempt from the 10-slot ring: evicting it would destroy the job on
        the platform (and its editable Smart Engine scene) while its layers
        still sit in Sliced/. The ring instead evicts the oldest OTHER file,
        so the queue still never exceeds 10."""
        try:
            with open(os.path.join(self.sliced_path, '.current_zip')) as f:
                return f.read().strip()
        except OSError:
            return ''

    def find_zip_files(self):
        """Find all ZIP and STL files in the uploads directory"""
        with self.lock:
            self.zip_files = []
            
            try:
                # Look for .zip and .stl files in the uploads directory
                if os.path.exists(self.uploads_path):
                    for file in os.listdir(self.uploads_path):
                        if file.lower().endswith(('.zip', '.stl')):
                            file_path = os.path.join(self.uploads_path, file)
                            if os.path.isfile(file_path):
                                # Make sure the file is complete and not being written
                                try:
                                    # Try to open the file to confirm it's not locked by another process
                                    with open(file_path, 'rb') as f:
                                        pass
                                    
                                    self.zip_files.append({
                                        'name': file,
                                        'path': file_path,
                                        'mtime': os.path.getmtime(file_path)
                                    })
                                except Exception as e:
                                    # Skip file if it can't be opened (likely still being written)
                                    logger.warning(f"Skipping file {file} - may still be uploading: {str(e)}")
                
                # Sort files by modification time (newest first)
                self.zip_files.sort(key=lambda x: x['mtime'], reverse=True)
                logger.debug(f"Found {len(self.zip_files)} files (ZIP and STL)")
                
                # Enforce the 10 file limit by deleting oldest files — the
                # armed/printing batch is exempt (see _ring_protected_name)
                if len(self.zip_files) > 10:
                    protected = self._ring_protected_name()
                    prot = [f for f in self.zip_files if f['name'] == protected]
                    rest = [f for f in self.zip_files if f['name'] != protected]
                    keep_n = 10 - len(prot)
                    files_to_delete = rest[keep_n:]

                    for file_info in files_to_delete:
                        try:
                            logger.info(f"Auto-deleting oldest file: {file_info['name']}")
                            os.remove(file_info['path'])
                        except Exception as e:
                            logger.error(f"Error auto-deleting file: {str(e)}")

                    # Update the list to keep only the 10 kept files
                    self.zip_files = sorted(prot + rest[:keep_n],
                                            key=lambda x: x['mtime'],
                                            reverse=True)
                
            except Exception as e:
                logger.error(f"Error finding files: {str(e)}")
    
    def _cleanup_old_files(self):
        """Clean up old files to maintain 10 file limit"""
        try:
            time.sleep(0.5)  # Small delay to ensure file write is complete
            
            # Read the directory and get files sorted by modification time
            all_files = []
            for f in os.listdir(self.uploads_path):
                if f.lower().endswith(('.zip', '.stl')):
                    f_path = os.path.join(self.uploads_path, f)
                    if os.path.isfile(f_path):
                        all_files.append({
                            'name': f,
                            'path': f_path,
                            'mtime': os.path.getmtime(f_path)
                        })
            
            # Sort by modification time (oldest first)
            all_files.sort(key=lambda x: x['mtime'])
            
            # If we have more than 10 files, remove the oldest ones — the
            # armed/printing batch is exempt (see _ring_protected_name)
            if len(all_files) > 10:
                protected = self._ring_protected_name()
                deletable = [f for f in all_files if f['name'] != protected]
                files_to_delete = deletable[:(len(all_files) - 10)]
                for old_file in files_to_delete:
                    try:
                        logger.info(f"Auto-deleting old file: {old_file['name']}")
                        os.remove(old_file['path'])
                    except Exception as e:
                        logger.error(f"Error deleting old file: {str(e)}")
        except Exception as e:
            logger.error(f"Error during file cleanup: {str(e)}")
        
    def update_movement_parameters_for_material(self, material_print_speed):
        """Update movement parameters based on material print speed"""
        try:
            if material_print_speed == 'fast':
                self.NORLAM_DECENT_SPEED = 3600
                self.NORMAL_LIFT_SPEED = 3600
                self.normal_raising_height = 6
                self.bottom_raising_height = 8
            elif material_print_speed == 'slow':
                self.NORLAM_DECENT_SPEED = 1600
                self.NORMAL_LIFT_SPEED = 1200
                self.normal_raising_height = 6
                self.bottom_raising_height = 8
            elif material_print_speed == 'high_viscosity':
                self.NORLAM_DECENT_SPEED = 1600
                self.NORMAL_LIFT_SPEED = 400
                self.normal_raising_height = 6
                self.bottom_raising_height = 8
            else:
                # Default to fast if unknown
                logger.warning(f"Unknown material_print_speed: {material_print_speed}, using 'slow' defaults")
                self.NORLAM_DECENT_SPEED = 1600
                self.NORMAL_LIFT_SPEED = 1200
                self.normal_raising_height = 6
                self.bottom_raising_height = 8
            
            logger.info(f"Updated movement parameters for material print speed: {material_print_speed}")
            logger.info(f"  NORLAM_DECENT_SPEED: {self.NORLAM_DECENT_SPEED}")
            logger.info(f"  NORMAL_LIFT_SPEED: {self.NORMAL_LIFT_SPEED}")
            logger.info(f"  normal_raising_height: {self.normal_raising_height}")
            logger.info(f"  bottom_raising_height: {self.bottom_raising_height}")
            
        except Exception as e:
            logger.error(f"Error updating movement parameters: {str(e)}")

    def extract_material_from_filename(self, filename):
        """Extract material name from sliced ZIP filename"""
        try:
            logger.info(f"Attempting to extract material from filename: {filename}")
            
            # Handle single STL format: {name}_Sliced({bundle}_{material}).zip
            if '_Sliced(' in filename and filename.endswith('.zip'):
                # Extract the part between the parentheses
                start = filename.find('_Sliced(')
                if start != -1:
                    start += len('_Sliced(')
                    end = filename.rfind(').zip')
                    if end != -1:
                        bundle_material = filename[start:end]
                        
                        # Get the current bundle name to properly split
                        current_bundle_basename = self.auto_slicer.get_current_bundle_basename()
                        
                        # Check if the bundle_material starts with the current bundle name
                        if bundle_material.startswith(current_bundle_basename):
                            # Remove the bundle name and the following underscore
                            material_name = bundle_material[len(current_bundle_basename):].lstrip('_')
                            logger.info(f"Extracted material name '{material_name}' from single STL filename '{filename}'")
                            return material_name
            
            # Handle multi-STL format: MultiModel_{count}parts_Sliced_{bundle}_{material}_.zip
            elif 'MultiModel_' in filename and 'parts_Sliced_' in filename and filename.endswith('_.zip'):
                # Find the start of the bundle_material part
                start = filename.find('parts_Sliced_')
                if start != -1:
                    start += len('parts_Sliced_')
                    # Remove the trailing '_.zip'
                    end = filename.rfind('_.zip')
                    if end != -1:
                        bundle_material = filename[start:end]
                        
                        # Get the current bundle name to properly split
                        current_bundle_basename = self.auto_slicer.get_current_bundle_basename()
                        
                        # Check if the bundle_material starts with the current bundle name
                        if bundle_material.startswith(current_bundle_basename):
                            # Remove the bundle name and the following underscore
                            material_name = bundle_material[len(current_bundle_basename):].lstrip('_')
                            logger.info(f"Extracted material name '{material_name}' from multi-STL filename '{filename}'")
                            return material_name
            
            # Fallback: try to match against known materials for both formats
            if not self.auto_slicer.available_materials:
                self.auto_slicer.parse_materials_from_bundle()
            
            # Find the longest matching material name from the filename
            for material in self.auto_slicer.available_materials:
                if material.name in filename:
                    logger.info(f"Extracted material name '{material.name}' from filename '{filename}' (fallback method)")
                    return material.name
            
            logger.warning(f"Could not extract material name from filename: {filename}")
            return None
            
        except Exception as e:
            logger.error(f"Error extracting material from filename: {str(e)}")
            return None
                
    def extract_zip_file(self, zip_path):
        """Extract a ZIP file to the destination folder"""
        
        # Block extraction during printing
        if self.is_printing:
            logger.warning("Cannot extract files while printing is in progress")
            self.status_message = "Cannot change files during printing"
            raise Exception("Cannot extract files while printing is in progress")
            
        # Also block during movement for extra safety
        if self.is_moving:
            logger.warning("Cannot extract files while platform is moving")
            self.status_message = "Cannot change files while platform is moving"
            raise Exception("Cannot extract files while platform is moving")
        
        try:
            # Guard the whole clear+rewrite of Sliced/ against a print start
            self.is_extracting = True
            # Create the extract path if it doesn't exist
            os.makedirs(self.sliced_path, exist_ok=True)
            zip_name = os.path.basename(zip_path)
            zip_basename = os.path.splitext(zip_name)[0]
            marker_file = os.path.join(self.sliced_path, '.current_zip')

	    # Clear the destination directory but preserve config files
            PRESERVE_FILES = ['prusaslicer.ini', '.current_bundle', '.current_zip']
            logger.info(f"Clearing destination directory: {self.sliced_path} (preserving config files)")

            for item in os.listdir(self.sliced_path):
                # Skip files we want to preserve
                if item in PRESERVE_FILES:
                    logger.debug(f"Preserving config file: {item}")
                    continue
                    
                item_path = os.path.join(self.sliced_path, item)
                if os.path.isfile(item_path):
                    os.unlink(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
            
            # Extract the ZIP file
            logger.info(f"Extracting {zip_name} to {self.sliced_path}")
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(self.sliced_path)

            # After successful extraction
            try:
                # Load print parameters
                if self.load_print_parameters():
                    # Calculate print time estimate
                    self.estimated_print_seconds, self.estimated_print_time = self.estimate_print_time()
                    logger.info(f"Print time estimation: {self.estimated_print_time}")
                    self.status_message = f"Extracted {zip_basename}. Est. print time: {self.estimated_print_time}"
            except Exception as e:
                logger.error(f"Error estimating print time: {str(e)}")

            # Create marker file to indicate the current extracted ZIP
            with open(marker_file, 'w') as f:
                f.write(zip_name)
            
            logger.info(f"Successfully extracted {zip_name}")
            
            # Load print parameters after extraction
            self.load_print_parameters()
            
        except Exception as e:
            logger.error(f"Error extracting ZIP file: {str(e)}")
            self.status_message = f"Error: {str(e)[:50]}"

            # Re-raise the exception so Flask route can handle it properly
            raise
        finally:
            self.is_extracting = False
            
    def find_png_base_name_and_count_slices(self):
        """Find the base name of PNG slice files and count actual slices"""
        try:
            if not os.path.exists(self.sliced_path):
                logger.error(f"Sliced path does not exist: {self.sliced_path}")
                return None, 0
            
            # Get all PNG files in the sliced directory
            png_files = [f for f in os.listdir(self.sliced_path) if f.lower().endswith('.png')]
            
            if not png_files:
                logger.warning("No PNG files found")
                return None, 0
            
            # Pattern to match files that end with exactly 5 digits - FIXED THE SYNTAX ERROR
            pattern = re.compile(r'^(.+?)(\d{5})\.png$', re.IGNORECASE)
            
            # Dictionary to store base names and their associated numbers
            base_name_files = {}
            
            for file in png_files:
                match = pattern.match(file)
                if match:
                    base_name = match.group(1)
                    number = int(match.group(2))
                    
                    # Skip common preview file patterns
                    if any(preview_pattern in base_name.lower() for preview_pattern in 
                           ['preview', 'thumb', 'thumbnail', 'cover', 'image']):
                        logger.info(f"Skipping preview file: {file}")
                        continue
                    
                    if base_name not in base_name_files:
                        base_name_files[base_name] = []
                    base_name_files[base_name].append(number)
            
            if not base_name_files:
                logger.warning("No valid PNG slice files found")
                return None, 0
            
            # For each base name, find the consecutive count starting from 0
            best_base_name = None
            best_count = 0
            
            for base_name, numbers in base_name_files.items():
                numbers.sort()
                logger.info(f"Base name '{base_name}' has files: {numbers[:10]}{'...' if len(numbers) > 10 else ''}")
                
                # Count consecutive files starting from 0
                consecutive_count = 0
                for i in range(len(numbers)):
                    if numbers[i] == consecutive_count:
                        consecutive_count += 1
                    else:
                        break
                
                logger.info(f"Base name '{base_name}' has {consecutive_count} consecutive slices starting from 0")
                
                # Choose the base name with the most consecutive slices
                if consecutive_count > best_count:
                    best_count = consecutive_count
                    best_base_name = base_name
            
            if best_base_name and best_count > 0:
                logger.info(f"Selected PNG base name: '{best_base_name}' with {best_count} consecutive slice files")
                return best_base_name, best_count
            else:
                logger.warning("No valid consecutive slice sequence found")
                return None, 0
                
        except Exception as e:
            logger.error(f"Error finding PNG base name and counting slices: {str(e)}")
            return None, 0
        
    def load_calibration(self):
        """Load calibration data from file if it exists"""
        config_file = matic_paths.STEPPER_CALIBRATION
        try:
            if os.path.exists(config_file):
                with open(config_file, 'r') as f:
                    data = json.load(f)
                    # Always start at home position on startup
                    self.current_position_mm = HOME_POSITION
                    logger.info(f"Calibration file found, but starting at home position: {self.current_position_mm}mm")
                    self.position_changed = True
            else:
                # Default to home position instead of 0.0
                self.current_position_mm = HOME_POSITION
                logger.info(f"No calibration file found, starting at home position: {HOME_POSITION}mm")
                self.position_changed = True
        except Exception as e:
            logger.error(f"Error loading calibration: {str(e)}")
            # On error, default to home position
            self.current_position_mm = HOME_POSITION
            self.position_changed = True
                
    def save_calibration(self):
        """Save calibration data to file"""
        config_file = matic_paths.STEPPER_CALIBRATION
        try:
            with open(config_file, 'w') as f:
                json.dump({'position': self.current_position_mm}, f)
            logger.info(f"Calibration saved: position = {self.current_position_mm}mm")
        except Exception as e:
            logger.error(f"Error saving calibration: {str(e)}")
    
    def setup_gpio(self):
        """Initialize GPIO pins for stepper motor control and UV light"""
        # Initialize the GPIO devices using gpiozero
        self.en_pin = DigitalOutputDevice(self.EN_PIN)
        self.ms1_pin = DigitalOutputDevice(self.MS1_PIN)
        self.ms2_pin = DigitalOutputDevice(self.MS2_PIN)
        self.step_pin = DigitalOutputDevice(self.STEP_PIN)
        self.dir_pin = DigitalOutputDevice(self.DIR_PIN)
        self.uv_light_pin = DigitalOutputDevice(self.UV_LIGHT_PIN)
        self.proximity_sensor = DigitalInputDevice(self.PROXIMITY_SENSOR_PIN, pull_up=False)

        
        # Set up microstepping mode (full step mode)
        self.ms1_pin.off()  # Equivalent to GPIO.output(self.MS1_PIN, GPIO.LOW)
        self.ms2_pin.off()  # Equivalent to GPIO.output(self.MS2_PIN, GPIO.LOW)
        
        # Turn off UV light initially
        self.uv_light_pin.off()  # Equivalent to GPIO.output(self.UV_LIGHT_PIN, GPIO.LOW)
        
        # Enable motor
        self.enable_motor()
    
    def setup_display(self):
        """Set up the display driver. On Pi 5 this auto-detects the panel and
        programs its mode at runtime; with no panel it comes up headless so the
        web UI/API stay reachable. create_display() never raises."""
        if self.display is None:
            self.display = create_display()
            self.display.clear_display()
        self.screen_connected = getattr(self.display, 'display_type', '') != 'none'
        if not self.screen_connected:
            logger.warning(
                "No display panel detected -- running headless. Slicing and "
                "printing are disabled until a screen is connected and the "
                "printer is restarted.")
        logger.info(f"Display initialized: {self.display.display_type}")
        logger.info(f"Horizontal scaling compensation factor: {self.horizontal_scale_factor}")
        logger.info(f"Horizontal offset: {self.horizontal_offset} pixels")

    def _require_screen(self):
        """Guard for screen-dependent endpoints. Returns a Flask (response,
        status) tuple when no LCD panel is present, else None. Slicing and
        printing produce nothing without the mask display, so we refuse them
        cleanly with a message the UI surfaces, instead of failing deep in the
        pipeline. MATIC_ALLOW_NO_SCREEN=1 lifts the gate on integration dev
        machines that have no panel wired (slice flows stay testable there)."""
        if os.environ.get('MATIC_ALLOW_NO_SCREEN') == '1':
            return None
        if not getattr(self, 'screen_connected', True):
            return jsonify({
                'success': False,
                'message': 'Screen not detected',
            }), 503
        return None
    
    def get_temperature(self):
        """Get the current system temperature of the Raspberry Pi in Celsius"""
        try:
            result = subprocess.run(['vcgencmd', 'measure_temp'], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                # Output format: "temp=XX.X'C"
                temp_str = result.stdout.strip().split('=')[1].replace("'C", "").strip()
                self.current_temperature = float(temp_str)
                return self.current_temperature
            else:
                logger.warning("Failed to read temperature: vcgencmd failed")
                return self.current_temperature  # Return last known temperature
        except Exception as e:
            logger.warning(f"Temperature read error: {e}")
            return self.current_temperature  # Return last known temperature
    
    def get_temperature_status(self):
        """Determine temperature status level: 'normal', 'warning', or 'critical'"""
        temp = self.get_temperature()
        if temp >= self.temp_critical_threshold:
            return 'critical'
        elif temp >= self.temp_warning_threshold:
            return 'warning'
        else:
            return 'normal'
    
    def log_temperature(self, context=""):
        """Log current temperature to the print log if printing"""
        if self.print_log_file and not self.print_log_file.closed and self.is_printing:
            temp = self.current_temperature
            status = self.get_temperature_status()
            timestamp = datetime.now().strftime('%H:%M:%S')
            log_msg = f"[{timestamp}] Temperature: {temp:.1f}°C (Status: {status}) {context}\n"
            try:
                self.print_log_file.write(log_msg)
                self.print_log_file.flush()
            except Exception as e:
                logger.error(f"Failed to log temperature: {e}")
    
    # Keep setup_gui as alias for compatibility
    def setup_gui(self):
        """Alias for setup_display (backward compatibility)"""
        self.setup_display()
    
    def enable_motor(self):
        """Enable the stepper motor"""
        self.en_pin.off()  # LOW enables the motor
        self.motor_enabled = True
        logger.info("Motor enabled")
        self.status_message = "Motor enabled"
    
    def disable_motor(self):
        """Disable the stepper motor"""
        self.en_pin.on()  # HIGH disables the motor
        self.motor_enabled = False
        logger.info("Motor disabled")
        self.status_message = "Motor disabled"
    
    def toggle_motor(self):
        """Toggle the stepper motor between enabled and disabled states"""
        if self.motor_enabled:
            self.disable_motor()
        else:
            self.enable_motor()
    
    def uv_light_on(self):
        """Turn on the UV light"""
        self.uv_light_pin.on()
        logger.info("UV light turned ON")

    def uv_light_off(self):
        """Turn off the UV light"""
        self.uv_light_pin.off()
        logger.info("UV light turned OFF")
                        
    # def set_zero_and_go_home(self):
    #     """Set current position as zero and move to home position"""
        
    #     if self.is_heating:
    #         logger.warning("Cannot set zero during resin heating")
    #         self.status_message = "Cannot set zero during resin heating"
    #         return False
        
    #     # First, make sure motor is enabled
    #     if not self.motor_enabled:
    #         self.enable_motor()
        
    #     # Now move Up by 0.1mm without updating the displayed position
    #     self.dir_pin.value = self.DIR_DOWN
        
    #     # Calculate steps for 0.1mm
    #     steps = int(0.1 * self.STEPS_PER_MM)
        
    #     # Default step delay
    #     step_delay = 0.00001
        
    #     # Perform steps without updating position
    #     for i in range(steps):
    #         self.step_pin.on()
    #         time.sleep(step_delay)
    #         self.step_pin.off()
    #         time.sleep(step_delay)
        
    #     logger.info("Platform moved Up by 0.1mm (not reflected in position)")
    #     time.sleep(0.5)

    #     # Set current position as zero
    #     self.current_position_mm = 0.0
    #     self.position_changed = True
    #     logger.info("Zero position set at current location")
    #     self.status_message = "Zero position set at current location"
        
    #     # Move platform to home position (100mm below zero)
    #     logger.info("Moving to home position (100mm below zero)")
    #     self.move_to_position(HOME_POSITION, speed_mm_min=self.move_home_speed)
        
    #     # Save calibration
    #     self.save_calibration()
    #     logger.info(f"Now at home position ({self.current_position_mm:.2f}mm)")
    #     self.status_message = f"At home position ({abs(self.current_position_mm):.2f}mm)"
    #     return True

    def move_down_until_sensor_trigger(self, max_distance_mm=150, speed_mm_min=200):
        """Move platform down until proximity sensor triggers or max distance reached"""
        if not self.motor_enabled:
            logger.warning("Cannot move - motor is disabled")
            self.status_message = "Cannot move - motor is disabled"
            return False
        
        logger.info(f"Moving towards sensor until it triggers (max {max_distance_mm}mm at {speed_mm_min}mm/min)")
        self.status_message = "Moving towards sensor to find zero position..."
        
        # Set movement flag
        self.is_moving = True
        
        try:
            # Check if sensor is accessible
            if not hasattr(self, 'proximity_sensor'):
                logger.error("Proximity sensor not initialized")
                return False
                
            # Set direction upward (towards sensor from home position)
            self.dir_pin.value = self.DIR_UP
            
            # Calculate step delay from speed
            step_delay = 1 / ((speed_mm_min / 60) * self.STEPS_PER_MM * 2)
            
            # Calculate maximum steps
            max_steps = int(max_distance_mm * self.STEPS_PER_MM)
            
            # Initial sensor state
            try:
                initial_state = self.proximity_sensor.is_active
                logger.info(f"Sensor initial state: {'HIGH' if initial_state else 'LOW'}")
            except Exception as e:
                logger.error(f"Cannot read sensor initial state: {e}")
                return False
            
            # Acceleration settings for smooth start
            accel_steps = 200  # Ramp up over first 200 steps (0.5mm)
            max_delay = step_delay * 5  # Start at 1/5th speed
            min_delay = step_delay  # Target speed
            
            # Move step by step until sensor triggers
            steps_moved = 0
            for step in range(max_steps):
                # Check sensor state every 10 steps (more frequent checking for immediate response)
                if step % 10 == 0:
                    try:
                        sensor_state = self.proximity_sensor.is_active
                        
                        # Log every 1000 steps to track progress
                        if step % 1000 == 0:
                            distance_moved = step / self.STEPS_PER_MM
                            logger.info(f"Moved {distance_moved:.2f}mm, sensor: {'HIGH' if sensor_state else 'LOW'}")
                        
                        # Check if sensor is triggered (HIGH signal = metal detected)
                        if sensor_state:
                            distance_moved = step / self.STEPS_PER_MM
                            logger.info(f"PROXIMITY SENSOR TRIGGERED after {distance_moved:.3f}mm")
                            # Dwell to let motor winding current decay before any direction change
                            time.sleep(0.5)
                            logger.info("Post-sensor dwell complete (0.5s)")
                            self.status_message = "Zero position found by sensor"
                            return True                            
                    except Exception as e:
                        logger.error(f"Error reading sensor at step {step}: {e}")
                        # Continue moving but log the error
                
                # Check for stop conditions
                if (self.should_stop and self.is_printing) or (self.should_stop_heating and self.is_heating):
                    logger.info("Movement stopped by user request")
                    return False
                
                # Check if GPIO devices are still valid before stepping
                try:
                    if hasattr(self.step_pin, 'closed') and self.step_pin.closed:
                        logger.error("Step pin closed during movement")
                        return False
                        
                    # Calculate delay with acceleration ramp
                    if step < accel_steps:
                        ratio = step / accel_steps
                        current_delay = max_delay - (max_delay - min_delay) * ratio
                    else:
                        current_delay = min_delay
                    
                    # Execute step
                    self.step_pin.on()
                    time.sleep(current_delay)
                    self.step_pin.off()
                    time.sleep(current_delay)                    
                    # Update position (moving upward towards sensor)
                    self.current_position_mm += 1 / self.STEPS_PER_MM
                    steps_moved = step + 1
                    
                except Exception as e:
                    logger.error(f"Error during step execution at step {step}: {e}")
                    return False
            
            # If we reach here, sensor didn't trigger within max distance
            distance_moved = steps_moved / self.STEPS_PER_MM
            logger.warning(f"Sensor did not trigger within {distance_moved:.2f}mm (max: {max_distance_mm}mm)")
            self.status_message = f"Sensor not triggered within {distance_moved:.1f}mm"
            return False
            
        except Exception as e:
            logger.error(f"Error during sensor-based movement: {str(e)}")
            self.status_message = f"Error: {str(e)[:50]}"
            return False
        
        finally:
            # Clear movement flag
            self.is_moving = False
                                        
    def move_down_100mm(self):
        """Move the platform down with sensor detection, stopping when sensor triggers"""
        try:
            if self.is_heating:
                logger.warning("Cannot move platform during resin heating")
                self.status_message = "Cannot move platform during resin heating"
                return
            
            # First, make sure motor is enabled
            if not self.motor_enabled:
                self.enable_motor()
            
            logger.info("Moving towards sensor with detection at 1200mm/min")
            self.status_message = "Moving towards sensor - will stop when sensor triggers"
            
            # Move down until sensor triggers with specified speed
            if self.move_down_until_sensor_trigger(max_distance_mm=100, speed_mm_min=1200):
                # Sensor was triggered - set this as zero position
                self.current_position_mm = 0.0
                self.position_changed = True
                self.save_calibration()
                logger.info("Sensor triggered - zero position set")
                self.status_message = f"Zero position set at sensor trigger point"
            else:
                # Sensor was not triggered within 100mm
                logger.warning("Sensor not triggered within 100mm")
                self.status_message = f"Sensor not triggered - at position {abs(self.current_position_mm):.2f}mm"
                
        except Exception as e:
            logger.error(f"Error during sensor-based movement: {str(e)}")
            self.status_message = f"Error: {str(e)[:50]}"
                        
    def heat_resin(self):
        """Heat resin by cycling UV light and moving platform for mixing"""
        if self.is_heating:
            return
        
        # Safety checks
        if self.is_printing or self.is_moving or self.is_cleaning:
            self.status_message = "Cannot heat resin while printing, moving, or cleaning"
            return
                
        if not self.motor_enabled:
            self.status_message = "Cannot heat resin - motor is disabled"
            return
        
        # Check current position and sensor state
        position_tolerance = 1.0
        at_home_position = abs(self.current_position_mm - HOME_POSITION) <= position_tolerance
        
        # Check if sensor is already triggered
        sensor_triggered = False
        try:
            if hasattr(self, 'proximity_sensor'):
                sensor_triggered = self.proximity_sensor.is_active
                logger.info(f"Sensor state before heating: {'TRIGGERED' if sensor_triggered else 'NOT TRIGGERED'}")
        except Exception as e:
            logger.error(f"Error reading sensor state: {e}")
            self.status_message = "Cannot heat resin - sensor error"
            return
        
        # Set heating state
        self.is_heating = True
        self.should_stop_heating = False
        self.heating_start_time = time.time()
        
        # Initialize display if it doesn't exist yet
        if self.display is None:
            self.setup_display()
        
        self.status_message = "Heating resin - warming and preparing"
        
        # Clear display to black
        self.clear_display()
        
        # START UV HEATING IMMEDIATELY
        self.uv_light_on()
        uv_light_currently_on = True
        
        # Start timing for the heating process
        start_time = time.time()
        uv_cycle_start = time.time()
        
        try:
            # SENSOR-BASED MOVEMENT TO ZERO POSITION
            if not sensor_triggered:
                # Platform needs to move to sensor position
                logger.info("Moving to sensor position for heating")
                self.status_message = "Heating resin - moving to sensor position"
                
                # Move up until sensor triggers
                if self.move_down_until_sensor_trigger(max_distance_mm=150, speed_mm_min=200):
                    # Sensor triggered successfully - set as zero position
                    self.current_position_mm = 0.0
                    self.position_changed = True
                    self.save_calibration()
                    logger.info("Sensor triggered - zero position set for heating")
                else:
                    # Sensor not triggered within range
                    logger.error("Sensor not triggered within 150mm - cannot heat resin")
                    self.status_message = "Heating failed - sensor not triggered"
                    self.uv_light_off()
                    self.is_heating = False
                    self.should_stop_heating = False
                    return
            else:
                # Sensor is already triggered - we're at zero position
                logger.info("Sensor already triggered - at zero position for heating")
                self.current_position_mm = 0.0
                self.position_changed = True
                self.save_calibration()
            
            # Check if heating was stopped during initial movement
            if self.should_stop_heating:
                logger.info("Heating stopped during initial movement to sensor position")
                self.status_message = "Heating stopped - returning to home position"
                return  # Will go to finally block
            
            # Now start the mixing cycles
            movement_cycle = 0
            
            while (time.time() - start_time) < self.heating_time and not self.should_stop_heating:
                
                # UV Light cycling: 30s on, 5s off
                current_time = time.time()
                cycle_time = current_time - uv_cycle_start
                
                if uv_light_currently_on and cycle_time >= 30:
                    self.uv_light_off()
                    uv_light_currently_on = False
                    uv_cycle_start = current_time
                elif not uv_light_currently_on and cycle_time >= 5:
                    self.uv_light_on()
                    uv_light_currently_on = True
                    uv_cycle_start = current_time
                
                # Platform movement: constant cycling between 0mm and -10mm
                movement_cycle += 1
                
                if movement_cycle % 2 == 1:
                    target_position = -10.0
                    speed = 200
                    direction = "up"
                else:
                    target_position = 0.0
                    speed = 200
                    direction = "down"
                
                # Check if we should stop before movement
                if self.should_stop_heating:
                    logger.info("Heating stop requested during mixing cycle")
                    break
                
                elapsed_time = time.time() - start_time
                if elapsed_time >= self.heating_time:
                    break
                
                remaining_time = self.heating_time - elapsed_time
                self.status_message = f"Heating resin - {remaining_time:.0f}s remaining"
                
                self.move_to_position(target_position, speed_mm_min=speed)
                
                # Check again after movement
                if self.should_stop_heating:
                    logger.info("Heating stop requested after movement")
                    break
                    
                if not self.should_stop_heating:
                    time.sleep(0.5)
            
            # Turn off UV light
            self.uv_light_off()
            uv_light_currently_on = False
            
            if not self.should_stop_heating:
                self.status_message = "Resin heating completed - returning to home"
            else:
                self.status_message = "Resin heating stopped - returning to home"
        
        except Exception as e:
            logger.error(f"Error during heating: {str(e)}")
            self.status_message = f"Error during heating: {str(e)[:50]}"
            try:
                self.uv_light_off()
                uv_light_currently_on = False
            except:
                pass
        
        finally:
            # CRITICAL: Turn off UV light first (safety)
            try:
                self.uv_light_off()
                logger.info("UV light turned off during heating cleanup")
            except Exception as e:
                logger.error(f"Error turning off UV light during cleanup: {e}")
            
            # Wait for any current movement to complete
            if self.is_moving:
                logger.info("Waiting for current movement to complete before returning home...")
                self.status_message = "Stopping heating - waiting for movement to complete..."
                timeout = 10  # 10 second timeout
                start_wait = time.time()
                while self.is_moving and (time.time() - start_wait) < timeout:
                    time.sleep(0.1)
                
                if self.is_moving:
                    logger.error("Movement timeout during heating stop - forcing clear")
                    self.is_moving = False
            
            # Set is_heating = False BEFORE returning to home position
            self.is_heating = False
            
            # Wait a moment to ensure heating loop has exited
            time.sleep(0.2)
            
            # Return to home position ONLY if no other movement is happening
            try:
                if not self.is_moving:
                    logger.info("Returning platform to home position after heating")
                    self.status_message = "Returning to home position..."
                    self.move_to_position(HOME_POSITION, speed_mm_min=self.move_home_speed)
                    
                    if self.should_stop_heating:
                        self.status_message = "Heating stopped - platform at home position"
                    else:
                        self.status_message = "Heating complete - platform at home position"
                else:
                    logger.warning("Skipping home position movement - another movement in progress")
                    self.status_message = "Heating stopped"
            except Exception as e:
                logger.error(f"Error returning to home position after heating: {str(e)}")
                self.status_message = f"Error returning home: {str(e)[:50]}"
            finally:
                # FINAL SAFETY CHECK - Ensure UV light is off
                try:
                    self.uv_light_off()
                    logger.info("Final UV light safety check - turned off")
                except Exception as e:
                    logger.error(f"Error in final UV light safety check: {e}")
            
            # Reset flags
            self.should_stop_heating = False
            
    def stop_heating(self):
        """Stop the resin heating process"""
        if self.is_heating:
            self.should_stop_heating = True
            self.status_message = "Stopping resin heating..."
            
            try:
                self.uv_light_off()
            except:
                pass
            
            time.sleep(0.5)
            
            try:
                self.uv_light_off()
            except:
                pass
        else:
            self.status_message = "No heating in progress"

    def clean_resin(self):
        """Clean resin by turning on UV light for 30 seconds with white screen"""
        if self.is_cleaning:
            return
        
        # Safety checks
        if self.is_printing or self.is_moving or self.is_heating:
            self.status_message = "Cannot clean while printing, moving, or heating"
            return
        
        # Set cleaning state
        self.is_cleaning = True
        self.should_stop_cleaning = False
        
        # Initialize display if it doesn't exist yet
        if self.display is None:
            self.setup_display()
        
        self.status_message = "Cleaning - UV light ON"
        
        # Display WHITE screen for cleaning (instead of clearing to black)
        self.display_white_screen()
        
        try:
            # Turn on UV light
            self.uv_light_on()
            
            # Wait for cleaning time (30 seconds) with countdown
            start_time = time.time()
            while (time.time() - start_time) < self.cleaning_time and not self.should_stop_cleaning:
                remaining_time = self.cleaning_time - (time.time() - start_time)
                self.status_message = f"Cleaning - {remaining_time:.0f}s remaining"
                time.sleep(1)
            
            # Turn off UV light
            self.uv_light_off()
            
            # Clear display after cleaning
            self.clear_display()
            
            if not self.should_stop_cleaning:
                self.status_message = "Cleaning completed"
            else:
                self.status_message = "Cleaning stopped"
        
        except Exception as e:
            self.status_message = f"Error during cleaning: {str(e)[:50]}"
            try:
                self.uv_light_off()
            except:
                pass
        
        finally:
            try:
                self.uv_light_off()
            except:
                pass
            
            # Clear display to black after cleaning
            self.clear_display()
            
            self.is_cleaning = False
            self.should_stop_cleaning = False

    def display_white_screen(self):
        """Display a white screen with 10mm black border for cleaning"""
        if self.display is None:
            self.setup_display()
        
        try:
            border_pixels = 100
            result = self.display.display_white_screen(border_pixels)
            
            if result:
                logger.info(f"Displayed white screen with {border_pixels}px (~10mm) black border for cleaning")
            return result
        except Exception as e:
            logger.error(f"Error displaying white screen: {str(e)}")
            return False
                        
    def stop_cleaning(self):
        """Stop the cleaning process"""
        if self.is_cleaning:
            self.should_stop_cleaning = True
            self.status_message = "Stopping cleaning..."
            
            try:
                self.uv_light_off()
            except:
                pass
        else:
            self.status_message = "No cleaning in progress"
            
    def calculate_white_ratio(self, image_path, img=None):
        """Calculate the ratio of white pixels to total pixels in an image.

        If `img` (an already-open PIL Image) is supplied, it is used instead
        of re-reading image_path. The print loop decodes each layer PNG once
        and passes it here AND to the display path, so a 9K layer (~12 MP) is
        decoded once per layer instead of twice."""
        try:
            # Open the image only if the caller didn't already decode it.
            if img is None:
                img = Image.open(image_path)

            # Grayscale for the pixel count WITHOUT mutating the shared image
            # (the display path still needs it in its original mode).
            gray = img if img.mode == 'L' else img.convert('L')

            # Use numpy for fast calculation
            arr = np.array(gray)
            white_count = np.sum(arr > 128)
            total_pixels = arr.size
            
            # Calculate percentage (0-100)
            ratio = int((white_count / total_pixels * 100) if total_pixels > 0 else 0) * self.ratio_multiplier
            
            logger.info(f"Layer white ratio: {ratio}% ({white_count} white pixels out of {total_pixels})")
            return ratio
            
        except Exception as e:
            logger.error(f"Error calculating white ratio: {str(e)}")
            return 0

    def detect_support_tip_transition(self, current_layer_ratio, previous_layer_ratio):
        """Detect if the current layer should use slow lift speed"""
        # Always log the ratios for debugging
        logger.info(f"SUPPORT DETECTION - Current layer {self.current_layer}: Current={current_layer_ratio}, Previous={previous_layer_ratio}")
        
        # Skip if this is the first layer or we don't have previous layer data
        if previous_layer_ratio is None:
            logger.info(f"SUPPORT DETECTION - Skipping (no previous layer data)")
            return False
        
        # Calculate the ratio increase from previous to current layer
        ratio_increase = current_layer_ratio - previous_layer_ratio
        
        logger.info(f"SUPPORT DETECTION - Ratio change = {ratio_increase} (threshold = {self.SUPPORT_TIP_RATIO_THRESHOLD})")
        
        # Detect if current layer has significantly more white pixels than previous
        if ratio_increase >= self.SUPPORT_TIP_RATIO_THRESHOLD:
            logger.info(f"SUPPORT TIP TRANSITION DETECTED!")
            logger.info(f"   Layer {self.current_layer}: Ratio increased by {ratio_increase}")
            logger.info(f"   From {previous_layer_ratio} to {current_layer_ratio}")
            logger.info(f"   Will use SLOW lift speed ({self.SUPPORT_TIP_LIFT_SPEED}mm/min) when lifting from this layer")
            return True
        
        logger.info(f"SUPPORT DETECTION - Normal layer (change={ratio_increase})")
        return False
                
    def move_to_position(self, target_position, speed_mm_min=None):
        """Move platform to an absolute Z position with acceleration and deceleration"""
        # Check if we should even start this movement
        if self.should_stop and self.is_printing:
            logger.info("Movement cancelled - stop requested")
            return

        # NEW: Check for heating stop requests
        if self.should_stop_heating and self.is_heating:
            logger.info("Movement cancelled - heating stop requested")
            return

        if self.is_cleaning:
            logger.warning("Movement blocked - cleaning in progress")
            return          

        # Set movement flag
        self.is_moving = True
        
        try:
            # Calculate distance and direction
            distance = target_position - self.current_position_mm
            
            if distance == 0:
                logger.info(f"Already at target position: {target_position:.4f}mm")
                return
                
            direction = self.DIR_UP if distance > 0 else self.DIR_DOWN
            
            # Default speeds
            if speed_mm_min is None:
                speed_mm_min = self.MOTOR_SPEED - self.white_ratio
                logger.debug("Using default speed_mm_min= %s", speed_mm_min)
            else:
                logger.info(f"Using provided speed_mm_min= {speed_mm_min}")

            logger.info(f"Moving from {self.current_position_mm:.4f}mm to {target_position:.4f}mm at {speed_mm_min}mm/min")
            
            # Acceleration settings
            accel_steps = self.move_home_speed + self.white_ratio * 3
            decel_steps = self.NORLAM_DECENT_SPEED + self.white_ratio * 3
            min_delay = 1 / ((speed_mm_min / 60) * self.STEPS_PER_MM * 2)
            max_delay = min_delay * 5
            logger.debug("accel_steps= %s, decel_steps= %s", accel_steps, decel_steps)
            
            # Calculate total steps
            total_steps = int(abs(distance) * self.STEPS_PER_MM)

            # Adjust acceleration for bottom layers
            if hasattr(self, 'current_layer') and self.current_layer <= self.bottom_layer_count:  
                accel_steps = int(total_steps * 0.3)
                decel_steps = int(total_steps * 0.3)
                logger.info(f"Using gentle acceleration for bottom layer {self.current_layer}")
            else:
                accel_steps = self.move_home_speed + self.white_ratio * 3
                decel_steps = self.NORLAM_DECENT_SPEED + self.white_ratio * 3
                
            # Adjust accel/decel steps if total steps is small
            if total_steps < (accel_steps + decel_steps + 100):
                accel_steps = total_steps // 4
                decel_steps = total_steps // 4
                
            logger.debug("accel_steps= %s, decel_steps= %s", accel_steps, decel_steps)
            
            # Set direction
            self.dir_pin.value = direction
            
            logger.info(f"Moving {'upward' if direction == self.DIR_UP else 'downward'} at {speed_mm_min}mm/min with acceleration")
            
            # Check for stop before starting movement
            if self.should_stop and self.is_printing:
                logger.info("Stop requested before movement start - aborting")
                return
            
            # NEW: Check for heating stop before starting movement
            if self.should_stop_heating and self.is_heating:
                logger.info("Heating stop requested before movement start - aborting")
                return
            
            # Perform steps with acceleration and deceleration.
            # loop_completed is set by the for-else clause and is True ONLY if
            # the loop ran every requested step without breaking. Any break
            # (stop, heating-stop, GPIO closed, step exception) leaves it False
            # so we can refuse to lie about the final position.
            loop_completed = False
            for i in range(total_steps):
                # Enhanced stop check - break immediately if stop requested
                if self.should_stop and self.is_printing:
                    logger.info("Stop requested during movement - completing current movement")
                    break

                # NEW: Check for heating stop during movement
                if self.should_stop_heating and self.is_heating:
                    logger.info("Heating stop requested during movement - aborting movement")
                    break

                # Also check if GPIO devices are still valid
                if hasattr(self.step_pin, 'closed') and self.step_pin.closed:
                    logger.warning("GPIO device closed during movement - stopping")
                    break

                # Calculate current delay for acceleration/deceleration
                if i < accel_steps:
                    # Accelerating
                    ratio = i / accel_steps
                    current_delay = max_delay - (max_delay - min_delay) * ratio
                elif i > (total_steps - decel_steps):
                    # Decelerating
                    ratio = (total_steps - i) / decel_steps
                    current_delay = max_delay - (max_delay - min_delay) * ratio
                else:
                    # Constant speed
                    current_delay = min_delay

                # Execute step (with error handling)
                try:
                    self.step_pin.on()
                    time.sleep(current_delay)
                    self.step_pin.off()
                    time.sleep(current_delay)
                except Exception as e:
                    logger.error(f"Error during step execution: {e}")
                    break

                # Update current position
                if direction == self.DIR_UP:
                    self.current_position_mm += 1 / self.STEPS_PER_MM
                else:
                    self.current_position_mm -= 1 / self.STEPS_PER_MM
            else:
                loop_completed = True

            # Snap to exact target only if every requested step was issued.
            # If the loop bailed early — for ANY reason, including step errors
            # and GPIO close — current_position_mm reflects the steps we
            # actually emitted; overwriting it to target would desync the
            # firmware's position model from physical reality and cause the
            # next movement to over- or under-travel.
            if loop_completed:
                self.current_position_mm = target_position
            else:
                logger.warning(
                    f"Movement aborted before completion; current_position_mm="
                    f"{self.current_position_mm:.4f}mm (intended target "
                    f"{target_position:.4f}mm)"
                )
                
            logger.info(f"Movement complete. Current position: {self.current_position_mm:.4f}mm")
            
        except Exception as e:
            logger.error(f"Error during movement: {e}")
            
        finally:
            # Clear movement flag
            self.is_moving = False
                                
    def display_image(self, image_path, preloaded_img=None):
        """Display an image on the LCD with horizontal scaling compensation and offset.

        `preloaded_img` lets the print loop hand over the PIL image it already
        decoded for the white-ratio pass so the backend skips a second
        Image.open of the same layer."""
        if self.display is None:
            self.setup_display()

        try:
            result = self.display.display_image(
                image_path,
                scale_factor=self.horizontal_scale_factor,
                offset_x=self.horizontal_offset,
                preloaded_img=preloaded_img
            )
            
            if result:
                logger.info(f"Displayed image: {image_path} (scale: {self.horizontal_scale_factor}, offset: {self.horizontal_offset}px)")
            return result
        except Exception as e:
            logger.error(f"Error displaying image: {str(e)}")
            return False
    
    def preload_next_image(self, image_path):
        """Pre-load the next image during current layer exposure"""
        if self.display is None:
            self.setup_display()
        
        return self.display.preload_image(
            image_path,
            scale_factor=self.horizontal_scale_factor,
            offset_x=self.horizontal_offset
        )
    
    def display_preloaded_image(self):
        """Display the pre-loaded image (very fast)"""
        if self.display is None:
            return False
        return self.display.display_preloaded()
    
    def clear_display(self):
        """Clear the display to black"""
        if self.display is not None:
            self.display.clear_display()
            self.current_image = None
            logger.info("Display cleared to black")
    
    def load_print_parameters(self):
        """Load print parameters from config.json or config.ini files"""
        try:
            # First try to find the PNG base name and count actual slices
            self.png_base_name, self.actual_slice_count = self.find_png_base_name_and_count_slices()
            if not self.png_base_name or self.actual_slice_count == 0:
                logger.error("Could not determine PNG base name or no slice files found")
                self.status_message = "Could not find valid slice files"
                return False
            
            # Try to load from config.json first
            if os.path.exists(self.config_json_file):
                logger.info(f"Loading parameters from {self.config_json_file}")
                return self._load_from_json()
            
            # Fall back to config.ini
            elif os.path.exists(self.config_ini_file):
                logger.info(f"Loading parameters from {self.config_ini_file}")
                return self._load_from_ini()
            
            else:
                logger.error("No config files found (config.json or config.ini)")
                self.status_message = "No config files found"
                return False
                
        except Exception as e:
            logger.error(f"Error loading print parameters: {str(e)}")
            self.status_message = f"Error loading parameters: {str(e)[:50]}"
            return False
    
    def _load_from_json(self):
        """Load parameters from config.json"""
        try:
            with open(self.config_json_file, 'r') as f:
                config = json.load(f)
            
            # Map PrusaSlicer parameters to our variables
            self.layer_height = float(config.get('layerHeight', 0.0))
            self.normal_exposure_time = float(config.get('expTime', 0.0))
            self.bottom_exposure_time = float(config.get('expTimeFirst', 0.0))
            self.bottom_layer_count = int(config.get('numFade', 0))
            
            # Calculate total layers: numFade (bottom) + numFast (normal)
            num_fast = int(config.get('numFast', 0))
            config_total_layers = self.bottom_layer_count + num_fast
            
            # Use the actual slice count, but validate against config
            if self.actual_slice_count != config_total_layers:
                logger.info(f"Mismatch: Config says {config_total_layers} layers, but found {self.actual_slice_count} slice files")
                logger.info(f"Using actual slice count: {self.actual_slice_count}")
            
            # Always use the actual slice count for total layers
            self.total_layers = self.actual_slice_count
            
            # Get print time estimate from config
            print_time_seconds = float(config.get('printTime', 0))
            if print_time_seconds > 0:
                self.estimated_print_seconds = print_time_seconds
                self.estimated_print_time = self._format_time(print_time_seconds)
            
            # Set default values for parameters not in PrusaSlicer config
            self.lift_height = 5.0  # Default lift height
            self.lift_speed = 300.0  # Default lift speed
            self.drop_speed = 300.0  # Default drop speed
            
            # Resolution comes from the active display backend, which knows
            # its own panel class. We refuse to guess: a missing display
            # driver here means setup_display() never ran or raised, and
            # silently substituting 9K dimensions would produce a wrong-
            # resolution print on a 4K panel.
            if not self.display:
                raise RuntimeError(
                    "Display backend not initialised - cannot derive print "
                    "resolution. Check setup_display() logs.")
            self.resolution_x = self.display.width
            self.resolution_y = self.display.height

            # Check prusaslicer.ini for material_print_speed
            prusaslicer_ini = os.path.join(self.sliced_path, 'prusaslicer.ini')
            if os.path.exists(prusaslicer_ini):
                try:
                    with open(prusaslicer_ini, 'r') as pf:
                        for line in pf:
                            if line.strip().startswith('material_print_speed'):
                                material_print_speed = line.split('=', 1)[1].strip()
                                if material_print_speed:
                                    logger.info(f"Found material_print_speed in prusaslicer.ini: {material_print_speed}")
                                    self.update_movement_parameters_for_material(material_print_speed)
                                break
                except Exception as e:
                    logger.error(f"Error reading prusaslicer.ini: {str(e)}")

            self._log_parameters()
            return True
            
        except Exception as e:
            logger.error(f"Error loading from config.json: {str(e)}")
            return False
    
    def _load_from_ini(self):
        """Load parameters from config.ini"""
        try:
            config = configparser.ConfigParser()
            config.read(self.config_ini_file)
            
            # All values are in DEFAULT section for INI format
            section = config['DEFAULT']
            
            # Map PrusaSlicer parameters to our variables
            self.layer_height = float(section.get('layerHeight', 0.0))
            self.normal_exposure_time = float(section.get('expTime', 0.0))
            self.bottom_exposure_time = float(section.get('expTimeFirst', 0.0))
            self.bottom_layer_count = int(section.get('numFade', 0))
            
            # Calculate total layers: numFade (bottom) + numFast (normal)
            num_fast = int(section.get('numFast', 0))
            config_total_layers = self.bottom_layer_count + num_fast
            
            # Use the actual slice count, but validate against config
            if self.actual_slice_count != config_total_layers:
                logger.warning(f"Mismatch: Config says {config_total_layers} layers, but found {self.actual_slice_count} slice files")
                logger.info(f"Using actual slice count: {self.actual_slice_count}")
            
            # Always use the actual slice count for total layers
            self.total_layers = self.actual_slice_count
            
            # Get print time estimate from config
            print_time_seconds = float(section.get('printTime', 0))
            if print_time_seconds > 0:
                self.estimated_print_seconds = print_time_seconds
                self.estimated_print_time = self._format_time(print_time_seconds)
            
            # Set default values for parameters not in PrusaSlicer config
            self.lift_height = 5.0  # Default lift height
            self.lift_speed = 300.0  # Default lift speed
            self.drop_speed = 300.0  # Default drop speed
            
            # Resolution comes from the active display backend (see
            # comment on the JSON-load path above for why we refuse to
            # silently substitute 9K dims if the display is None).
            if not self.display:
                raise RuntimeError(
                    "Display backend not initialised - cannot derive print "
                    "resolution. Check setup_display() logs.")
            self.resolution_x = self.display.width
            self.resolution_y = self.display.height

            # Read material print speed from PrusaSlicer config and update movement parameters
            material_print_speed = section.get('material_print_speed', '')
            if material_print_speed:
                logger.info(f"Found material_print_speed in config.ini: {material_print_speed}")
                self.update_movement_parameters_for_material(material_print_speed)

            self._log_parameters()
            return True
            
        except Exception as e:
            logger.error(f"Error loading from config.ini: {str(e)}")
            return False
    
    def _log_parameters(self):
        """Log the loaded parameters"""
        logger.info("Print parameters loaded successfully:")
        logger.info(f"PNG Base Name: {self.png_base_name}")
        logger.info(f"Actual Slice Count: {self.actual_slice_count}")
        logger.info(f"Layer Height: {self.layer_height}mm")
        logger.info(f"Bottom Layer Count (numFade): {self.bottom_layer_count}")
        logger.info(f"Bottom Exposure Time (expTimeFirst): {self.bottom_exposure_time}s")
        logger.info(f"Normal Exposure Time (expTime): {self.normal_exposure_time}s")
        logger.info(f"Normal Layers (numFast): {self.total_layers - self.bottom_layer_count}")
        logger.info(f"Total Layers (using actual slice count): {self.total_layers}")
        logger.info(f"Resolution: {self.resolution_x}x{self.resolution_y}")
        logger.info(f"Estimated Print Time: {self.estimated_print_time}")
        logger.info(f"Support Tip Detection Threshold: {self.SUPPORT_TIP_RATIO_THRESHOLD}")
        logger.info(f"Support Tip Lift Speed: {self.SUPPORT_TIP_LIFT_SPEED}mm/min")
        
        self.status_message = "Print parameters loaded"
    
    def _format_time(self, seconds):
        """Format time in seconds to a human-readable string"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        seconds = int(seconds % 60)
        
        if hours > 0:
            return f"{hours}h {minutes}m"
        elif minutes > 0:
            return f"{minutes}m {seconds}s"
        else:
            return f"{seconds}s"
    
    def estimate_print_time(self):
        """Calculate estimated print time based on layers and exposure times"""
        try:
            if self.total_layers == 0:
                return 0, "Unknown"
            
            # Use the time from config if available
            if hasattr(self, 'estimated_print_seconds') and self.estimated_print_seconds > 0:
                return self.estimated_print_seconds, self.estimated_print_time
            
            # Calculate based on exposure times
            bottom_time = self.bottom_layer_count * self.bottom_exposure_time
            normal_layers = self.total_layers - self.bottom_layer_count
            normal_time = normal_layers * self.normal_exposure_time
            
            # Add time for movements (estimate 10 seconds per layer for lifting/lowering)
            movement_time = self.total_layers * 10
            
            total_seconds = bottom_time + normal_time + movement_time
            formatted_time = self._format_time(total_seconds)
            
            return total_seconds, formatted_time
            
        except Exception as e:
            logger.error(f"Error estimating print time: {str(e)}")
            return 0, "Unknown"
    
    def get_layer_image_path(self, layer_number):
        """Get the full path to a layer image file using the PrusaSlicer naming convention"""
        # Layer numbers start from 1, but file numbers start from 0
        file_number = layer_number - 1
        
        # Check if this layer number is within our actual slice count
        if file_number >= self.actual_slice_count:
            logger.warning(f"Layer {layer_number} (file number {file_number}) exceeds actual slice count {self.actual_slice_count}")
            return None
        
        # Format the 5-digit suffix
        suffix = f"{file_number:05d}"
        
        # Construct the filename
        filename = f"{self.png_base_name}{suffix}"
        
        # Try different extensions (.png is most common)
        for ext in ['.png', '.PNG']:
            layer_path = os.path.join(self.sliced_path, filename + ext)
            if os.path.exists(layer_path):
                return layer_path
        
        # Try without extension (in case the base name already includes it)
        layer_path = os.path.join(self.sliced_path, filename)
        if os.path.exists(layer_path):
            return layer_path
        
        logger.warning(f"Layer file not found: {filename}")
        return None
    
    def parse_skip_layers_from_notes(self, material_notes):
        """Parse number of layers to skip from material notes"""
        if not material_notes:
            return 0
        
        try:
            # Convert to lowercase for case-insensitive matching
            notes_lower = material_notes.lower()
            
            # Look for patterns like:
            # "(skip first 3 layers)", "(skip 10 layers)", "(skip first 10 layers)"
            # "skip first 5 layers", "skip 7 layers", etc.
            
            # Pattern 1: (skip first X layers) or (skip X layers)
            pattern1 = r'\(skip\s+(?:first\s+)?(\d+)\s+layers?\)'
            match = re.search(pattern1, notes_lower)
            if match:
                skip_count = int(match.group(1))
                logger.info(f"Found skip instruction: {skip_count} layers (pattern: parentheses)")
                return skip_count
            
            # Pattern 2: skip first X layers or skip X layers (without parentheses)
            pattern2 = r'skip\s+(?:first\s+)?(\d+)\s+layers?'
            match = re.search(pattern2, notes_lower)
            if match:
                skip_count = int(match.group(1))
                logger.info(f"Found skip instruction: {skip_count} layers (pattern: no parentheses)")
                return skip_count
            
            # Pattern 3: start from layer X (implies skipping X-1 layers)
            pattern3 = r'start\s+from\s+layer\s+(\d+)'
            match = re.search(pattern3, notes_lower)
            if match:
                start_layer = int(match.group(1))
                skip_count = start_layer - 1
                logger.info(f"Found start instruction: start from layer {start_layer} (skipping {skip_count} layers)")
                return skip_count
            
            logger.info("No skip instruction found in material notes")
            return 0
            
        except Exception as e:
            logger.error(f"Error parsing skip layers from notes: {str(e)}")
            return 0

    def start_print(self):
        """Start the printing process with automatic sensor-based calibration"""
        if self.is_printing:
            logger.info("Printing already in progress")
            return

        # Reset stale print_start_time immediately so UI doesn't use old value
        self.print_start_time = None
        
        # ENHANCED SAFETY CHECK: Wait for any previous stop operation to complete
        if self.is_stopping:
            logger.warning("Previous print is still stopping - waiting...")
            timeout = 15  # 15 second timeout
            start_time = time.time()
            while self.is_stopping and (time.time() - start_time) < timeout:
                time.sleep(0.1)
            
            if self.is_stopping:
                logger.error("Previous print stop timeout - cannot start new print")
                self.status_message = "Cannot start print - previous print still stopping"
                return
        
        # DOUBLE-CHECK: Ensure no print is actually running by checking UV light state
        if hasattr(self, 'uv_light_pin') and self.uv_light_pin.value:
            logger.warning("UV light is on - another print may be running")
            self.status_message = "Cannot start print - UV light is active"
            return
        
        # SAFETY CHECK 1: Motor must not be moving
        if self.is_moving:
            logger.warning("Cannot start print while motor is moving")
            self.status_message = "Cannot start print - motor is moving"
            return

        # SAFETY CHECK 2: Platform must be at Home Position or sensor already triggered
        position_tolerance = 1.0
        at_home_position = abs(self.current_position_mm - HOME_POSITION) <= position_tolerance
        
        # Check if sensor is already triggered (platform at zero position)
        sensor_triggered = False
        try:
            if hasattr(self, 'proximity_sensor'):
                sensor_triggered = self.proximity_sensor.is_active
                logger.info(f"Sensor state before print: {'TRIGGERED' if sensor_triggered else 'NOT TRIGGERED'}")
        except Exception as e:
            logger.error(f"Error reading sensor state: {e}")
            self.status_message = "Cannot start print - sensor error"
            return
        
        if not (at_home_position or sensor_triggered):
            logger.warning(f"Cannot start print from current position: {self.current_position_mm:.2f}mm")
            logger.warning(f"Platform must be at Home Position ({HOME_POSITION}mm) or sensor must be triggered")
            self.status_message = f"Cannot start print from {abs(self.current_position_mm):.1f}mm - move to Home position first"
            return
        
        # SAFETY CHECK 3: Motor must be enabled
        if not self.motor_enabled:
            logger.warning("Cannot start print - motor is disabled")
            self.status_message = "Cannot start print - motor is disabled"
            return
        
        # Initialize starting layer and skipped layers
        starting_layer = 1
        self.layers_skipped = 0
        
        # Load material-specific parameters and check for skip layers
        try:
            marker_file = os.path.join(self.sliced_path, '.current_zip')
            if os.path.exists(marker_file):
                with open(marker_file, 'r') as f:
                    current_zip_name = f.read().strip()
                    
                    # Extract material name from filename
                    material_name = self.extract_material_from_filename(current_zip_name)
                    
                    if material_name:
                        # Find the material in the current bundle
                        if not self.auto_slicer.available_materials:
                            self.auto_slicer.parse_materials_from_bundle()
                        
                        for material in self.auto_slicer.available_materials:
                            if material.name == material_name:
                                # self.ratio_multiplier = material.ratio_multiplier  # Doesn't exist
                                # self.normal_raising_height = material.raising_distance  # Doesn't exist  
                                # self.heating_time = material.heating_time  # Doesn't exist
                                
                                # Reset white ratio values for new print
                                self.white_ratio = 0
                                self.current_layer_white_ratio = 0
                                self.next_layer_white_ratio = 0
                                
                                # Update movement parameters (this sets raising heights based on material_print_speed)
                                self.update_movement_parameters_for_material(material.material_print_speed)
                                self.status_message = f"Print parameters updated for {material_name} ({material.material_print_speed})"
                                
                                # Check for skip layers instruction in material notes
                                layers_to_skip = self.parse_skip_layers_from_notes(material.material_notes)
                                if layers_to_skip > 0:
                                    self.layers_skipped = layers_to_skip
                                    starting_layer = layers_to_skip + 1
                                    logger.info(f"Material notes: Skipping first {layers_to_skip} layers, starting from layer {starting_layer}")
                                    self.status_message = f"Starting from layer {starting_layer} (skipping first {layers_to_skip} layers) - {material_name}"
                                break
        except Exception as e:
            logger.error(f"Error checking for material parameters: {e}")
            # Continue with default parameters
        
        # Check if we still have layers to print
        if starting_layer > self.actual_slice_count:
            logger.warning(f"Starting layer {starting_layer} exceeds total layers {self.actual_slice_count}")
            self.status_message = "Print already completed"
            return
        
        # Log print start information
        if at_home_position:
            logger.info(f"Starting print from Home Position ({self.current_position_mm:.2f}mm)")
            self.status_message = "Starting print from Home Position"
        elif sensor_triggered:
            logger.info(f"Starting print from sensor position (sensor already triggered)")
            self.status_message = "Starting print from sensor position"
        
        # Record print start time
        self.print_start_time = time.time()
        
        # Start print log file
        try:
            # Get current zip name to extract filenames
            filenames = []
            marker_file = os.path.join(self.sliced_path, '.current_zip')
            if os.path.exists(marker_file):
                with open(marker_file, 'r') as f:
                    current_zip_name = f.read().strip()
                    filenames.append(current_zip_name)
            else:
                filenames.append("Unknown file")
            
            # Start the print log
            self.start_print_log(filenames)
            self.log_print(f"Print started from {'Home Position' if at_home_position else 'sensor position'}")
            self.log_print(f"Total layers: {self.actual_slice_count}")
            if self.layers_skipped > 0:
                self.log_print(f"Skipping first {self.layers_skipped} layers, starting from layer {starting_layer}")
        except Exception as e:
            logger.error(f"Error starting print log: {e}")

        # Initialize display if it doesn't exist yet
        if self.display is None:
            self.setup_display()
        
        # Set printing state
        self.is_printing = True
        self.should_stop = False
        self.current_layer = starting_layer
        
        # Reset white ratio values for UI
        self.current_layer_white_ratio = 0
        self.next_layer_white_ratio = 0
        
        # Clear the display first
        self.clear_display()
        
        # Ensure UV light is off at the start
        self.uv_light_off()
        
        # Update status message
        if self.layers_skipped > 0:
            self.status_message = f"Print started - moving to sensor position (skipping first {self.layers_skipped} layers)"
        else:
            self.status_message = "Print started - moving to sensor position"
        
        try:
            # SENSOR-BASED MOVEMENT LOGIC
            if at_home_position:
                # Platform is at home, need to move up until sensor triggers
                logger.info("Moving from Home Position to sensor trigger point")
                self.log_print("Moving from Home Position to sensor trigger point")
                self.status_message = "Moving to sensor position..."
                
                # Move up until sensor triggers (max 150mm)
                if self.move_down_until_sensor_trigger(max_distance_mm=150, speed_mm_min=800):
                    # Sensor triggered successfully - set as zero position
                    self.current_position_mm = 0.0
                    self.position_changed = True
                    self.save_calibration()
                    logger.info("Sensor triggered - zero position set, starting print")
                    self.log_print("Sensor triggered - zero position set, starting print")
                    self.status_message = "Sensor position found - starting print"
                else:
                    # Sensor not triggered within range
                    logger.error("Sensor not triggered within 150mm - cannot start print")
                    self.log_print("ERROR: Sensor not triggered within 150mm - cannot start print")
                    self.status_message = "Print failed - sensor not triggered"
                    self.close_print_log()
                    self.is_printing = False
                    self.should_stop = False
                    return
            
            elif sensor_triggered:
                # Sensor is already triggered - we're at zero position
                logger.info("Sensor already triggered - setting zero position")
                self.log_print("Sensor already triggered - setting zero position")
                self.current_position_mm = 0.0
                self.position_changed = True
                self.save_calibration()
                self.status_message = "At sensor position - starting print"

            # Platform is now at zero position (sensor triggered)
            # Process each layer
            while self.current_layer <= self.actual_slice_count and not self.should_stop:
                # Calculate VIRTUAL layer number
                virtual_layer = self.current_layer - self.layers_skipped
                
                # Log layer processing
                logger.info(f"Processing Layer {self.current_layer}/{self.actual_slice_count}")
                self.log_print(f"Processing Layer {self.current_layer}/{self.actual_slice_count}")
                if self.layers_skipped > 0:
                    logger.info(f"  (Virtual layer {virtual_layer} in positioning)")
                    self.log_print(f"  (Virtual layer {virtual_layer} in positioning)")
                
                # Get the current layer image path
                layer_path = self.get_layer_image_path(self.current_layer)
                
                # Check if the current layer exists
                if not layer_path:
                    logger.error(f"Error: Layer file not found for layer {self.current_layer}")
                    self.log_print(f"ERROR: Layer file not found for layer {self.current_layer}")
                    self.status_message = f"Error: Layer {self.current_layer} file not found"
                    break

                # Decode this layer's PNG exactly once and share it between the
                # white-ratio pass and the framebuffer render. On a 9K panel
                # each layer is a ~12 MP image; opening it twice per layer was
                # pure CPU/allocation waste on the 2 GB Pi. On any decode error
                # layer_img stays None and both consumers fall back to their own
                # Image.open(layer_path), preserving the original behaviour.
                layer_img = None
                try:
                    layer_img = Image.open(layer_path)
                    layer_img.load()
                except Exception as e:
                    logger.error(f"Could not preload layer {self.current_layer} image: {e}")

                # Calculate white pixel ratio for this layer
                final_white_ratio = self.calculate_white_ratio(layer_path, img=layer_img)
                self.current_layer_white_ratio = final_white_ratio
                self.white_ratio = final_white_ratio
                logger.info(f"Layer {self.current_layer} (virtual layer {virtual_layer}) white ratio: {final_white_ratio}")
                self.log_print(f"Layer {self.current_layer} (virtual layer {virtual_layer}) white ratio: {final_white_ratio}")

                # Display the current layer image (reusing the decode above)
                if self.display_image(layer_path, preloaded_img=layer_img):
                    # Delay before displaying the layer
                    time.sleep(0.5)  
                    # Turn on UV light after image is displayed
                    self.uv_light_on()
                
                # Determine exposure time based on VIRTUAL layer
                if virtual_layer <= self.bottom_layer_count:
                    exposure_time = self.bottom_exposure_time
                else:
                    exposure_time = self.normal_exposure_time
                                
                # Update status on web UI (simple message)
                self.status_message = f"Exposing layer {self.current_layer}/{self.actual_slice_count}"

                # Wait for exposure WITH stop checking only (pause waits for exposure to complete)
                # Exposure starts - no verbose logging needed

                # Enhanced exposure with stop checking every 0.1 seconds
                # Note: pause_requested does NOT interrupt exposure - it waits for layer completion
                exposure_start_time = time.time()
                while (time.time() - exposure_start_time) < exposure_time and not self.should_stop:
                    time.sleep(0.1)

                # Check if exposure was interrupted
                actual_exposure_time = time.time() - exposure_start_time
                if self.should_stop:
                    logger.info(f"Exposure interrupted by stop request after {actual_exposure_time:.1f}s")
                    self.log_print(f"Exposure interrupted by stop request after {actual_exposure_time:.1f}s")
                else:
                    logger.info(f"Exposure completed: {actual_exposure_time:.1f}s")
                    self.log_print(f"Exposure completed: {actual_exposure_time:.1f}s")
                    # Log temperature after exposure
                    self.log_temperature(f"(Layer {self.current_layer})")
                    if self.pause_requested:
                        logger.info(f"Pause will activate after this layer completes")
                        self.log_print(f"Pause will activate after this layer completes")

                # Turn off UV light after exposure
                self.uv_light_off()
		
                # Small delay to ensure display is fully black before UV turns off
                time.sleep(0.2)
                
                # Clear display after exposure
                self.clear_display()
                
                # Check if pause was requested - handle it AFTER layer completes
                if self.pause_requested and not self.should_stop:
                    logger.info(f"Pause requested - layer {self.current_layer} completed, moving to home position")
                    self.log_print(f"Pause requested - layer {self.current_layer} completed, moving to home position")
                    self.status_message = "Pausing - moving to home position"
                    
                    # Move platform to home position
                    self.move_to_position(HOME_POSITION)
                    
                    # Set paused state and clear processing flag
                    self.is_paused = True
                    self.pause_requested = False
                    self.pause_processing = False
                    self.status_message = f"Print paused at layer {self.current_layer}/{self.actual_slice_count}"
                    logger.info(f"Print paused at layer {self.current_layer}, platform at home position")
                    self.log_print(f"Print paused at layer {self.current_layer}, platform at home position")
                    
                    # Wait until resume or stop
                    while self.is_paused and not self.should_stop:
                        time.sleep(0.1)
                    
                    # If resumed, move to next layer position and continue
                    if not self.should_stop:
                        # Increment to next layer BEFORE resuming
                        self.current_layer += 1

                        logger.info(f"Resuming print - moving to layer {self.current_layer} position")
                        self.log_print(f"Resuming print - moving to layer {self.current_layer} position")
                        self.status_message = "Resuming - moving to print position"

                        # Calculate the position layer N+1 should be exposed at.
                        # Mirrors the normal end-of-iter descend which uses the
                        # just-completed virtual_layer (i.e., current_layer - 1
                        # after the increment above), NOT the new one — otherwise
                        # the resumed layer prints one layer_height too far from
                        # the screen.
                        next_layer_position = self.layer_height * (self.current_layer - 1 - self.layers_skipped)
                        logger.info(f"Moving to virtual layer {self.current_layer - self.layers_skipped} position: {next_layer_position:.4f}mm")
                        self.log_print(f"Moving to virtual layer {self.current_layer - self.layers_skipped} position: {next_layer_position:.4f}mm")
                        
                        # Move directly to the next layer position at 1600mm/min (fast resume)
                        self.move_to_position(next_layer_position * -1, speed_mm_min=1600)
                        
                        logger.info(f"At layer position, resuming print from layer {self.current_layer}")
                        self.log_print(f"At layer position, resuming print from layer {self.current_layer}")
                        self.status_message = f"Resuming print from layer {self.current_layer}"
                        
                        # Allow 1 second for resin to settle before activating UV
                        logger.info("Waiting 1 second for resin to settle before UV activation")
                        self.log_print("Waiting 1 second for resin to settle before UV activation")
                        time.sleep(1.0)
                        
                        # Continue to process this layer - the normal loop will handle it
                        continue
                
                # Only move platform if we have another layer to process AND stop/pause not active
                if (self.current_layer < self.actual_slice_count and 
                    not self.should_stop and 
                    self.is_printing and
                    not self.is_paused):
                    
                    # Get the NEXT layer's path and white ratio for UI display
                    next_layer_number = self.current_layer + 1
                    next_layer_path = self.get_layer_image_path(next_layer_number)
                    
                    if not next_layer_path:
                        logger.warning(f"Next layer {next_layer_number} not found, stopping print")
                        self.log_print(f"WARNING: Next layer {next_layer_number} not found, stopping print")
                        break
                    
                    # Calculate the NEXT layer's white ratio for UI display only
                    next_white_ratio = self.calculate_white_ratio(next_layer_path)
                    self.next_layer_white_ratio = next_white_ratio
                    next_virtual_layer = next_layer_number - self.layers_skipped
                    logger.info(f"Next layer {next_layer_number} (virtual layer {next_virtual_layer}) white ratio: {next_white_ratio}")
                    self.log_print(f"Next layer {next_layer_number} (virtual layer {next_virtual_layer}) white ratio: {next_white_ratio}")
                    
                    # Calculate raising height based on VIRTUAL next layer
                    if next_virtual_layer <= 5:
                        raising_height = self.bottom_raising_height
                    else:
                        raising_height = self.normal_raising_height

                    # Determine lifting speed
                    lift_speed = None
                    speed_reason = ""
                    
                    if next_virtual_layer <= self.bottom_layer_count:
                        lift_speed = self.BOTTOM_LIFT_SPEED
                        speed_reason = f"bottom virtual layer {next_virtual_layer}"
                    else:
                        # Check for support tip transition
                        if virtual_layer > 1:
                            prev_layer_path = self.get_layer_image_path(self.current_layer - 1)
                            if prev_layer_path:
                                prev_white_ratio = self.calculate_white_ratio(prev_layer_path)
                                logger.info(f"Previous layer {self.current_layer - 1} white ratio: {prev_white_ratio}")
                                
                                if self.detect_support_tip_transition(self.current_layer_white_ratio, prev_white_ratio):
                                    lift_speed = self.SUPPORT_TIP_LIFT_SPEED
                                    speed_reason = f"support tip transition detected"
                                else:
                                    lift_speed = self.NORMAL_LIFT_SPEED
                                    speed_reason = f"normal layer lift"
                            else:
                                lift_speed = self.NORMAL_LIFT_SPEED
                                speed_reason = f"normal layer lift (no prev layer data)"
                        else:
                            lift_speed = self.NORMAL_LIFT_SPEED
                            speed_reason = f"first virtual layer lift"
                    
                    logger.info(f"Using {lift_speed}mm/min lift speed for {speed_reason}")
                    self.log_print(f"Using {lift_speed}mm/min lift speed for {speed_reason}")
                    
                    # Platform movement calculations use VIRTUAL layer
                    # Move platform up
                    up_position = ((self.layer_height * virtual_layer) + raising_height + int(self.current_layer_white_ratio / 20)) * -1
                    logger.info(f"Moving platform up to: {up_position:.4f}mm at {lift_speed}mm/min")
                    self.log_print(f"Moving platform up to: {up_position:.4f}mm at {lift_speed}mm/min")
                    
                    # Update status message based on speed type
                    if lift_speed == self.BOTTOM_LIFT_SPEED:
                        self.status_message = f"Lifting platform from layer {self.current_layer} (slow - bottom layer)"
                    elif lift_speed == self.SUPPORT_TIP_LIFT_SPEED:
                        self.status_message = f"Lifting platform from layer {self.current_layer} (slow - support tip)"
                    else:
                        self.status_message = f"Lifting platform from layer {self.current_layer} (normal)"
                    
                    self.move_to_position(up_position, speed_mm_min=lift_speed)
                    
                    # Check again after lift movement
                    if self.should_stop or not self.is_printing:
                        logger.info("Stop requested after lift - exiting print loop")
                        self.log_print("Stop requested after lift - exiting print loop")
                        break
                    
                    # Move platform down to next virtual layer position
                    next_virtual_layer_position = self.layer_height * virtual_layer
                    logger.info(f"Moving platform down to virtual layer {next_virtual_layer} position: {next_virtual_layer_position:.4f}mm")
                    self.log_print(f"Moving platform down to virtual layer {next_virtual_layer} position: {next_virtual_layer_position:.4f}mm")
                    self.status_message = f"Lowering platform for layer {next_layer_number}"
                    self.move_to_position(next_virtual_layer_position * -1, speed_mm_min=self.NORLAM_DECENT_SPEED)
                
                # Check one final time before incrementing layer
                if self.should_stop or not self.is_printing:
                    logger.info("Stop requested - exiting print loop before layer increment")
                    self.log_print("Stop requested - exiting print loop before layer increment")
                    break
                    
                # Increment layer counter for next iteration
                self.current_layer += 1

            # Printing complete or stopped
            if not self.should_stop:
                logger.info("Printing completed successfully")
                self.log_print("Printing completed successfully")
                
                # Calculate total print time
                print_end_time = time.time()
                total_print_time_seconds = print_end_time - self.print_start_time
                formatted_total_time = self._format_time(total_print_time_seconds)
                
                # Update status message with total print time
                self.status_message = f"Print complete - Total time: {formatted_total_time}"
                self.log_print(f"Print complete - Total time: {formatted_total_time}")

                # Update persistent operating time stats
                print_minutes = int(total_print_time_seconds // 60)
                total_prints = self.get_total_print_count()
                total_minutes = self.get_total_print_minutes() + print_minutes
                self.save_print_stats(total_prints, total_minutes)
                
                # Move platform to home position after printing
                logger.info("Moving platform to home position after print completion")
                self.log_print("Moving platform to home position after print completion")
                self.move_to_position(HOME_POSITION)            
                
            else:
                logger.info("Printing stopped by user")
                self.log_print("Printing stopped by user")
                self.status_message = "Print stopped"
        
        except Exception as e:
            logger.error(f"Error during printing: {str(e)}")
            self.log_print(f"ERROR during printing: {str(e)}")
            self.status_message = f"Error during printing: {str(e)[:50]}"
        
        finally:
            # Close the print log file
            self.close_print_log()
            
            # Make sure UV light is off when printing ends
            self.uv_light_off()
            
            # Reset printing state and UI values
            self.is_printing = False
            self.is_paused = False
            self.pause_requested = False
            self.current_layer_white_ratio = 0
            self.next_layer_white_ratio = 0
    
    def pause_print(self):
        """Request to pause the printing process after current layer completes"""
        if self.is_printing and not self.is_paused and not self.pause_requested:
            self.pause_requested = True
            self.pause_processing = True
            logger.info("Pause requested - will pause after current layer completes")
            self.status_message = "Pausing after current layer"
    
    def resume_print(self):
        """Resume a paused print"""
        if self.is_paused:
            logger.info("Resume requested")
            self.is_paused = False
            self.pause_processing = False
            self.status_message = "Resuming print"
                                                            
    # Flag to prevent multiple stop operations
    _stop_in_progress = False
                        
    def stop_print(self):
        """Stop the printing process and return to home position"""
        # Prevent multiple stop operations from being processed simultaneously
        if self._stop_in_progress:
            logger.info("Stop operation already in progress")
            return
            
        if self.is_printing or self.is_paused:
            # Set the flag to indicate stop operation is in progress
            self.__class__._stop_in_progress = True
            
            # Set stopping flag to prevent file operations during entire stop sequence
            self.is_stopping = True
            
            # Clear pause state if paused
            if self.is_paused:
                self.is_paused = False
                self.pause_requested = False
                self.pause_processing = False
            
            self.should_stop = True
            logger.info("Stopping print...")
            self.status_message = "Stopping print..."
            
            # Turn off UV light immediately when stopping - FIRST SAFETY CHECK
            try:
                self.uv_light_off()
            except Exception as e:
                logger.error(f"Error turning off UV light (first attempt): {e}")
            
            # Wait for any current movement to complete
            if self.is_moving:
                logger.info("Waiting for current movement to complete...")
                self.status_message = "Stopping print - waiting for movement to complete..."
                while self.is_moving and self.should_stop:
                    time.sleep(0.1)  # Wait in small increments
            
            # CRITICAL FIX: Wait for print loop to actually exit before setting is_printing = False
            logger.info("Waiting for print loop to exit...")
            timeout = 10  # 10 second timeout
            start_time = time.time()
            
            # Keep checking if print loop is still running by monitoring layer changes
            last_layer = self.current_layer
            loop_exit_detected = False
            
            while (time.time() - start_time) < timeout and not loop_exit_detected:
                time.sleep(0.1)
                
                # Check if print loop has exited by monitoring if layers stop changing
                # and checking internal state
                current_time = time.time()
                if (current_time - start_time) > 2:  # After 2 seconds, check layer progression
                    if self.current_layer == last_layer:
                        # Layer hasn't changed for a while, print loop likely exited
                        loop_exit_detected = True
                    else:
                        last_layer = self.current_layer
            
            if not loop_exit_detected:
                logger.warning("Print loop exit timeout - forcing stop")
            
            # SECOND SAFETY CHECK - Ensure UV light is off after loop exit
            try:
                self.uv_light_off()
            except Exception as e:
                logger.error(f"Error turning off UV light (second attempt): {e}")
            
            # NOW it's safe to set is_printing = False
            self.is_printing = False
            logger.info("Print loop confirmed stopped, is_printing set to False")
            
            # CRITICAL: Wait a moment to ensure print loop has fully exited
            time.sleep(0.5)  # Give print loop time to see is_printing=False and exit
            
            # Double-check that no movement is happening before proceeding
            if self.is_moving:
                logger.warning("Movement still in progress after stop request - waiting longer...")
                timeout = 10  # 10 second timeout
                start_time = time.time()
                while self.is_moving and (time.time() - start_time) < timeout:
                    time.sleep(0.1)
                
                if self.is_moving:
                    logger.error("Movement timeout - forcing stop")
                    self.is_moving = False  # Force clear the flag
            
            # Return to home position ONLY if no other movement is happening
            try:
                if not self.is_moving:
                    logger.info("Moving platform to home position")
                    self.status_message = "Moving to home position..."
                    self.move_to_position(HOME_POSITION)
                    self.status_message = "Print stopped, platform at home position"
                else:
                    logger.warning("Skipping home position movement - another movement in progress")
                    self.status_message = "Print stopped"
            except Exception as e:
                logger.error(f"Error returning to home position: {str(e)}")
                self.status_message = f"Error returning to home: {str(e)[:50]}"
            finally:
                # THIRD SAFETY CHECK - Ensure UV light is off after all operations
                try:
                    self.uv_light_off()
                    logger.info("UV light safety check - turned off")
                except Exception as e:
                    logger.error(f"Error in final UV light safety check: {e}")
            
            # Reset UI values
            self.current_layer_white_ratio = 0
            self.next_layer_white_ratio = 0
            
            # Reset ALL flags - including the new stopping flag
            self.is_stopping = False  # NEW: Clear stopping flag
            self.__class__._stop_in_progress = False
            self.print_start_time = None  # Clear stale timer

        else:
            logger.info("No print in progress")
            self.status_message = "No print in progress"
                                        
    def on_close(self):
        """Clean up resources when program is closed"""
        self.stop_print()
        self.cleanup()
        
    def exit_program(self):
        """Exit the program cleanly"""
        logger.info("Exiting program...")
        
        # Stop and cleanup MATIC Cure
        try:
            if self.cure:
                self.cure.cleanup()
        except Exception as e:
            logger.warning(f"Error during Cure cleanup on exit: {e}")
        
        # Stop any ongoing print FIRST (but don't cleanup GPIO yet)
        if self.is_printing:
            self.should_stop = True
            self.is_printing = False
            logger.info("Stopping print for exit...")
            
            # Wait for print loop to exit
            timeout = 5
            start_time = time.time()
            while self.is_moving and (time.time() - start_time) < timeout:
                time.sleep(0.1)
        
        # Turn off UV light (safely)
        try:
            if hasattr(self, 'uv_light_pin') and not self.uv_light_pin.closed:
                self.uv_light_off()
        except Exception as e:
            logger.warning(f"Could not turn off UV light during exit: {e}")
        
        # Disable motor to save power (safely)
        try:
            if hasattr(self, 'en_pin') and not self.en_pin.closed:
                self.disable_motor()
        except Exception as e:
            logger.warning(f"Could not disable motor during exit: {e}")
        
        # Close GPIO devices safely
        try:
            gpio_devices = ['en_pin', 'ms1_pin', 'ms2_pin', 'step_pin', 'dir_pin', 'uv_light_pin']
            for device_name in gpio_devices:
                if hasattr(self, device_name):
                    device = getattr(self, device_name)
                    if hasattr(device, 'closed') and not device.closed:
                        device.close()
                        logger.debug(f"Closed {device_name}")
            logger.info("GPIO cleanup complete")
        except Exception as e:
            logger.warning(f"Error during GPIO cleanup: {e}")
        
        # Clean up display
        try:
            if self.display:
                self.display.cleanup()
        except Exception as e:
            logger.warning(f"Error cleaning up display: {e}")
        
        try:
            stop_discovery_service()
        except:
            pass

        # Force exit the program after a short delay
        import os
        import threading
        
        # Create a timer that will force exit after 1 second
        threading.Timer(1.0, lambda: os._exit(0)).start()
        
        logger.info("Exit requested, terminating in 1.0 seconds...")

    def is_slicing_active(self):
        """Check if any slicing operations are currently active"""
        return len(self.auto_slicer.active_slicing_operations) > 0

    def cleanup(self):
        """Clean up resources"""
        # Cleanup MATIC Cure
        try:
            if self.cure:
                self.cure.cleanup()
        except:
            pass
        
        try:
            # Turn off UV light
            self.uv_light_off()
        except:
            pass  # Ignore errors if device is already closed
        
        try:
            # Disable motor to save power
            self.disable_motor()
        except:
            pass  # Ignore errors if device is already closed
        
        # Close gpiozero devices
        try:
            self.en_pin.close()
        except:
            pass
        try:
            self.ms1_pin.close()
        except:
            pass
        try:
            self.ms2_pin.close()
        except:
            pass
        try:
            self.step_pin.close()
        except:
            pass
        try:
            self.dir_pin.close()
        except:
            pass
        try:
            self.uv_light_pin.close()
        except:
            pass
        try:
            self.proximity_sensor.close()
        except:
            pass
        logger.info("GPIO cleanup complete")
        try:
            stop_discovery_service()
        except:
            pass
    
    def _load_print_stats_from_disk(self):
        """Read print_count.txt from SD into cached counters. Called on demand
        the first time stats are needed, and never again — all subsequent
        reads/writes go through the cached values, with one batched SD write
        at the end of each print."""
        count_file = os.path.join(self.log_path, 'print_count.txt')
        total_prints = 0
        total_minutes = 0
        try:
            if os.path.exists(count_file):
                with open(count_file, 'r') as f:
                    data = {}
                    for line in f:
                        if '=' in line:
                            key, val = line.strip().split('=', 1)
                            data[key] = val
                    total_prints = int(data.get('total_prints', 0))
                    total_minutes = int(data.get('total_print_minutes', 0))
            else:
                # Legacy bootstrap: count existing log files once.
                log_files = glob.glob(os.path.join(self.log_path, '*.log'))
                total_prints = len(log_files)
        except Exception as e:
            logger.error(f"Error reading print stats: {e}")
        self._cached_total_prints = total_prints
        self._cached_total_print_minutes = total_minutes

    def get_total_print_count(self):
        """Get the total number of prints (cached, no SD read after startup)."""
        if self._cached_total_prints is None:
            self._load_print_stats_from_disk()
        return self._cached_total_prints

    def get_total_print_minutes(self):
        """Get the total printing time in minutes (cached, no SD read after startup)."""
        if self._cached_total_print_minutes is None:
            self._load_print_stats_from_disk()
        return self._cached_total_print_minutes

    def save_print_stats(self, total_prints, total_minutes):
        """Update cached counters and persist to SD. The persist step is the
        single SD write batched at the end of each print — do not call this
        on a hot path."""
        self._cached_total_prints = total_prints
        self._cached_total_print_minutes = total_minutes
        count_file = os.path.join(self.log_path, 'print_count.txt')
        try:
            hours = total_minutes // 60
            mins = total_minutes % 60
            with open(count_file, 'w') as f:
                f.write(f"total_prints={total_prints}\n")
                f.write(f"total_print_minutes={total_minutes}\n")
            logger.info(f"Print stats saved: {total_prints} prints, {hours} hours and {mins} minutes total")
        except Exception as e:
            logger.error(f"Error saving print stats: {e}")
    
    def delete_old_logs(self):
        """Delete log files older than 30 days"""
        try:
            cutoff_date = datetime.now() - timedelta(days=30)
            log_files = glob.glob(os.path.join(self.log_path, '*.log'))
            deleted_count = 0
            
            for log_file in log_files:
                try:
                    file_mtime = datetime.fromtimestamp(os.path.getmtime(log_file))
                    if file_mtime < cutoff_date:
                        os.remove(log_file)
                        deleted_count += 1
                        logger.info(f"Deleted old log file: {os.path.basename(log_file)}")
                except Exception as e:
                    logger.error(f"Error deleting log file {log_file}: {e}")
            
            if deleted_count > 0:
                logger.info(f"Deleted {deleted_count} log files older than 30 days")
        except Exception as e:
            logger.error(f"Error in delete_old_logs: {e}")
    
    def start_print_log(self, filenames):
        """Start a new print log file in tmpfs. Nothing is written to the SD
        card here — the finished log is copied to SD by close_print_log()."""
        try:
            # Increment cached print count in memory only — no SD write yet.
            print_count = self.get_total_print_count() + 1
            self._cached_total_prints = print_count

            # Create log filename with format: DD.MM.YYYY(count).log
            now = datetime.now()
            log_filename = f"{now.day}.{now.month}.{now.year}({print_count}).log"
            self.print_log_final_path = os.path.join(self.log_path, log_filename)
            self.print_log_tmpfs_path = os.path.join(self.tmpfs_log_path, log_filename)

            # Open log file in tmpfs with default buffering — no per-line flushes.
            self.print_log_file = open(self.print_log_tmpfs_path, 'w')

            # Write header with print info
            header = f"{'='*80}\n"
            header += f"MATIC Print Log - Print #{print_count}\n"
            header += f"Started: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
            header += f"{'='*80}\n\n"
            header += f"Files in print process:\n"
            for filename in filenames:
                header += f"  - {filename}\n"
            header += f"\n{'='*80}\n\n"

            self.print_log_file.write(header)

            logger.info(f"Started print log (tmpfs): {log_filename}")
            return self.print_log_tmpfs_path
        except Exception as e:
            logger.error(f"Error starting print log: {e}")
            self.print_log_file = None
            self.print_log_tmpfs_path = None
            self.print_log_final_path = None
            return None

    def log_print(self, message):
        """Write a message to the active tmpfs print log file. No SD writes
        and no per-line flush — Python's default buffering is fine because
        the file lives in RAM."""
        try:
            if self.print_log_file and not self.print_log_file.closed:
                timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                log_message = f"[{timestamp}] {message}\n"
                self.print_log_file.write(log_message)
        except Exception as e:
            logger.error(f"Error writing to print log: {e}")

    def close_print_log(self):
        """Finalize the print log: write footer, close the tmpfs file, then
        perform the *single* batched SD write of this print — copy the log to
        SD, persist updated stats, fsync, and prune old logs. If the power is
        cut before this runs, the in-progress log is lost (intentional)."""
        tmpfs_path = self.print_log_tmpfs_path
        final_path = self.print_log_final_path
        try:
            if self.print_log_file and not self.print_log_file.closed:
                footer = f"\n{'='*80}\n"
                footer += f"Print log ended: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                footer += f"{'='*80}\n"
                self.print_log_file.write(footer)
                self.print_log_file.flush()
                self.print_log_file.close()
        except Exception as e:
            logger.error(f"Error finalizing tmpfs print log: {e}")
        finally:
            self.print_log_file = None

        # Batched SD work — only if we actually have a finished tmpfs log.
        if tmpfs_path and final_path and os.path.exists(tmpfs_path):
            try:
                shutil.copyfile(tmpfs_path, final_path)
                # fsync the file and the directory so the copy survives a power cut.
                try:
                    with open(final_path, 'rb') as f:
                        os.fsync(f.fileno())
                    dir_fd = os.open(self.log_path, os.O_DIRECTORY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except Exception as e:
                    logger.error(f"fsync after log copy failed: {e}")

                # Persist the cached stats now (single SD write for the counter file).
                self.save_print_stats(
                    self._cached_total_prints or 0,
                    self._cached_total_print_minutes or 0,
                )

                # Prune old logs after the new one is safely on SD.
                self.delete_old_logs()

                logger.info(f"Print log copied to SD: {final_path}")
            except Exception as e:
                logger.error(f"Error copying print log to SD: {e}")
            finally:
                # Always remove the tmpfs copy so /run doesn't accumulate stale files.
                try:
                    os.remove(tmpfs_path)
                except Exception:
                    pass

        self.print_log_tmpfs_path = None
        self.print_log_final_path = None

        # Record a local diagnostics snapshot at print end. This batches into
        # the same SD-write window as the print log copy + counter update.
        try:
            diag = health.collect_diagnostics()
            severity = health.severity_from_diagnostics(diag)
            health.record_local_diagnostics(diag, severity, reason='print_end')
            self._last_recorded_severity = severity
        except Exception as e:
            logger.error(f"Failed to record diagnostics at print end: {e}")

    def _diagnostics_timer_loop(self):
        """Background thread: every 30 minutes, collect diagnostics and
        append a row to the local history file. Also writes a row immediately
        whenever severity transitions (green<->yellow<->red), so the moment
        a problem starts is captured even if it falls between the regular
        30-min ticks. The first iteration runs after a short warm-up so it
        doesn't compete with display init / Flask startup."""
        # Brief warm-up so the first read happens after the system has settled.
        time.sleep(60)
        while True:
            try:
                diag = health.collect_diagnostics()
                severity = health.severity_from_diagnostics(diag)
                last = getattr(self, '_last_recorded_severity', None)
                if severity != last:
                    health.record_local_diagnostics(diag, severity, reason='severity_change')
                    self._last_recorded_severity = severity
                else:
                    health.record_local_diagnostics(diag, severity, reason='periodic')
            except Exception as e:
                logger.error(f"Diagnostics timer iteration failed: {e}")
            # 30 minute cadence. Sleep in 60s slices so service stop
            # responds quickly during a `systemctl restart`.
            for _ in range(30):
                time.sleep(60)

    def run(self):
        """Main run method"""
        logger.info("MATIC LCD Printer with Enhanced Material Selection started")
        
        if self.ip_address == "0.0.0.0":
            logger.info("Web interface available on all network interfaces at port 5000")
            logger.info("Try: http://matic.local:5000 or check your router for the Pi's IP")
        else:
            logger.info(f"Web interface available at http://{self.ip_address}:{self.port}")    
        
        # Initialize display (auto-detects 9K or 4K)
        self.setup_display()
        
        # Run the Flask web server (blocking - this is now the main loop)
        try:
            self.app.run(host='0.0.0.0', port=self.port, threaded=True)
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
        except Exception as e:
            logger.error(f"Error running Flask app: {str(e)}")
        finally:
            self.cleanup()
    
    def cleanup(self):
        """Clean up resources"""
        try:
            self.uv_light_off()
        except:
            pass
        F
        # Clean up display
        try:
            if self.display:
                self.display.cleanup()
        except:
            pass
        
        try:
            stop_discovery_service()
        except:
            pass
        
        logger.info("Cleanup completed")


# Run the application if this is the main module
if __name__ == "__main__":
    printer = MaticLCDPrinter()
    printer.run()
