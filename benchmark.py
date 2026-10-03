#!/usr/bin/env python3
"""Run paired comparisons. Keep the original commands and Python imports."""
import sys
from runner.contracts import (FEATURES,METRICS,balanced_repetitions,comparison_reasons,
    number,schedule,selected_schedule,validate_measurement,validate_plan)
from runner.evidence import check_inputs,digest,evidence_hashes,object_hash,read,write
from runner.execution import host_class,host_identity,invoke,merge,run,stop_group
from runner.reporting import display,display_interval,make_report,neutral_comparisons
from runner.statistics import absolute_interval,bootstrap_interval,neutral_statistics,paired_interval,percentile
from runner.cli import main

if __name__=='__main__':sys.exit(main())
