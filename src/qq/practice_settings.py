"""Shared judge budgets, including queueing and restart recovery."""
import json
from pathlib import Path

DEFAULTS={
 'reasoning_effort':'max',
 'max_tokens':32768,
 'request_timeout':300,
 'total_timeout':660,
 'operation_timeout':900,
}

def grading_options(settings=None):
    if settings is None:
        path=Path.home()/'.local/share/hermes-acm/practice/grading.json'
        settings=json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(settings,dict):raise ValueError('invalid grading settings')
    result=dict(DEFAULTS,**settings)
    if result['reasoning_effort'] not in {'low','high','max'}:raise ValueError('invalid reasoning effort')
    bounds={'max_tokens':(1024,131072),'request_timeout':(1,900),'total_timeout':(1,3600),'operation_timeout':(1,7200)}
    for name,(low,high) in bounds.items():
        value=result[name]
        if type(value) is not int or not low<=value<=high:raise ValueError('invalid grading limit')
    if result['total_timeout']<result['request_timeout']:raise ValueError('request exceeds total timeout')
    if result['operation_timeout']<result['total_timeout']+30:raise ValueError('operation lacks queue allowance')
    model=result.get('fallback_model')
    if model is not None and (not isinstance(model,str) or not model or len(model)>150):raise ValueError('invalid fallback model')
    return result
