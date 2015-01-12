#!/usr/local/bin/env python

#=============================================================================================
# MODULE DOCSTRING
#=============================================================================================

"""
Analyze YANK output file.

"""

#=============================================================================================
# MODULE IMPORTS
#=============================================================================================


#=============================================================================================
# COMMAND DISPATCH
#=============================================================================================

def dispatch(args):
    from yank import analyze
    analyze.analyze(args['--store'], verbose=args['--verbose'])
    return True