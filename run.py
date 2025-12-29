#!/usr/bin/env python
"""
Aria Launcher
Runs aria module with proper package context.
"""
import sys
import os

# Ensure project root is in path (so 'aria' package can be found)
# The aria/ folder inside project root has __init__.py that redirects imports
project_root = os.path.dirname(os.path.abspath(__file__))

if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Fix sys.argv[0] for argparse
sys.argv[0] = os.path.join(project_root, "aria")

from aria.app import main

main()
