"""Read-only lab admission and clear progress from existing study state."""
from . import studies,host_control
from .evidence import read,write

def doctor(profile,lab,output):
    config=read(lab)
    if 'host_control' not in config:raise ValueError('lab doctor needs the shared host_control configuration')
    host_control.validate(config['host_control'],check_files=True)
    with host_control.reservation(config['host_control']):
        resources=host_control.resources(config['host_control'],output.parent)
        manifest=studies.prepare(profile,lab,output)
        probe=output/'admission';probe.mkdir()
        write(probe/'resources.json',resources)
        snapshot=host_control.command(config['host_control'],'snapshot',probe)
        write(output/'ready-lab.json',{'prepared_plan':str(output/'base-plan.json'),'host_control':read(output/'base-plan.json')['host_control']})
    result={'passed':True,'lab':str(output/'ready-lab.json'),'prepared_plan':str(output/'base-plan.json'),'expected_trials':manifest['expected'],
            'resources':resources,'host_snapshot':snapshot,
            'next_action':'Run study collect with ready-lab.json and the selected profile. No traffic or CI change was made.',
            'limits':['Runtime host quality, generator capacity, and memory measurement boundaries still need their declared checks.']}
    write(output/'doctor.json',result)
    return result

def progress(result):
    phase=result.get('phase');checks=result.get('checks') or {};collection=result.get('collection') or {}
    if phase in ('check_failed','cleanup_failed','complete_with_failures'):
        action='Review the saved failures. Keep this output. Use a new output after a correction.'
    elif phase=='complete':action='Review raw evidence and comparison gates before publication.'
    elif phase in ('ready_to_report',):action='Resume without a trial limit to write reports and verify the archive.'
    else:action='Resume this saved workflow to continue its declared checks and trials.'
    return {**result,'next_action':action,'progress':{'checks':checks.get('checked_trials',0),
        'expected_checks':checks.get('expected_checks',0),'trials':collection.get('collected',0),
        'expected_trials':collection.get('expected',0),'failed':result.get('failed',0)}}
