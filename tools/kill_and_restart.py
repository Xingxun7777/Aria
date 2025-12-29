#!/usr/bin/env python3
"""Kill existing Aria processes and restart cleanly."""

import subprocess
import time
import os
import tempfile
import sys

# Find and kill Aria processes
print("=== Aria Process Manager ===")

# Use wmic to find python processes
result = subprocess.run(
    [
        "wmic",
        "process",
        "where",
        "name like '%python%'",
        "get",
        "processid,commandline",
    ],
    capture_output=True,
    text=True,
)

print("Current Python processes:")
print(result.stdout)

# Find Aria PID
aria_pids = []
for line in result.stdout.split("\n"):
    if "aria" in line.lower():
        parts = line.strip().split()
        if parts:
            try:
                pid = int(parts[-1])
                aria_pids.append(pid)
                print(f"  Found Aria process: PID {pid}")
            except ValueError:
                pass

# Kill found processes
for pid in aria_pids:
    print(f"Killing PID {pid}...")
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)

# Remove lock file
lock_file = os.path.join(tempfile.gettempdir(), "aria.lock")
if os.path.exists(lock_file):
    try:
        os.remove(lock_file)
        print(f"Removed lock file: {lock_file}")
    except Exception as e:
        print(f"Could not remove lock file: {e}")
else:
    print("No lock file found")

time.sleep(1)

# Now try to start Aria with console output
print("\n=== Starting Aria with console output ===")
# Use the directory where this script is located
script_dir = os.path.dirname(os.path.abspath(__file__))
project_dir = os.path.dirname(script_dir)
os.chdir(project_dir)

# Use python.exe (not pythonw.exe) to see console output
python_exe = sys.executable
launcher = os.path.join(project_dir, "launcher.py")

print(f"Running: {python_exe} {launcher}")
print("-" * 50)

# Run and capture output
proc = subprocess.Popen(
    [python_exe, launcher],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    bufsize=1,
)

# Read output for 30 seconds or until process exits
import threading


def read_output():
    for line in proc.stdout:
        print(line, end="")


thread = threading.Thread(target=read_output)
thread.daemon = True
thread.start()

# Wait for some output
print("Waiting for startup output (30 seconds)...")
thread.join(timeout=30)

print("\n" + "-" * 50)
if proc.poll() is None:
    print("Aria is running. Press Ctrl+C to stop monitoring.")
else:
    print(f"Process exited with code: {proc.returncode}")
