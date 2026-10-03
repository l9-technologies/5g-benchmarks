"""Statistical calculations. No file access or candidate-specific rules."""
import math
import random
import statistics

def percentile(values, p):
    values = sorted(values)
    return values[max(0, math.ceil(p * len(values)) - 1)]


def paired_interval(a, b, seed):
    """Paired bootstrap CI for mean relative change, in percent. No zero division."""
    if any(v == 0 for v in b):
        return [0.0, 0.0] if a == b else None
    deltas = [100 * (x - y) / y for x, y in zip(a, b, strict=True)]
    return bootstrap_interval(deltas, seed)


def absolute_interval(a, b, seed):
    return bootstrap_interval([x - y for x, y in zip(a, b, strict=True)], seed)


def bootstrap_interval(deltas, seed):
    if min(deltas) == max(deltas):
        return [deltas[0], deltas[0]]
    rng = random.Random(seed)
    means = [statistics.mean(rng.choices(deltas, k=len(deltas))) for _ in range(4000)]
    return [percentile(means, .025), percentile(means, .975)]


def neutral_statistics(a,b,seed,equivalence_percent,direction):
    # Linear quantiles preserve interval sign when candidate A and B are exchanged.
    def interval(deltas):
        rng=random.Random(seed)
        means=sorted(statistics.mean(rng.choices(deltas,k=len(deltas))) for _ in range(4000))
        def quantile(q):
            x=q*(len(means)-1);lo=math.floor(x);hi=math.ceil(x)
            return means[lo]*(hi-x)+means[hi]*(x-lo) if hi!=lo else means[lo]
        return [quantile(.025),quantile(.975)]
    absolute=interval([x-y for x,y in zip(a,b,strict=True)])
    relative=interval([200*(x-y)/(abs(x)+abs(y)) if x or y else 0 for x,y in zip(a,b,strict=True)])
    margin=(abs(statistics.median(a))+abs(statistics.median(b)))/2*equivalence_percent/100
    decision='no_clear_difference'
    if max(abs(v) for v in absolute)<=margin:decision='equivalent'
    elif absolute[0]>margin:decision='b_better' if direction=='lower' else 'a_better'
    elif absolute[1]<-margin:decision='a_better' if direction=='lower' else 'b_better'
    return {'absolute_interval':absolute,'symmetric_percent_interval':relative,'margin':margin,'decision':decision}
