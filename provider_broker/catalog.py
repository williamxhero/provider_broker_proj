"""Fixed, explicit capability catalog. Unknown models are never inferred."""
CATALOG = {
    'luna': ('standard', 1), 'terra': ('smart', 3), 'sonnet': ('smart', 3),
    'sol': ('expert', 6), 'opus5': ('expert', 7), 'opus-5': ('expert', 7),
    'opus4.8': ('expert', 7), 'opus-4.8': ('expert', 7),
}
def classify(model):
    lower=model.lower().replace('_','-')
    for token,value in CATALOG.items():
        if token in lower: return value
    return None
