"""
Collectors are imported by module, not re-exported here.

Deliberate, and the opposite of what envelope and landing_zone do. Those two are
contracts every other package crosses, so a single curated surface is what keeps
their internals free to move. Collectors are leaves: each one imports a client
library of its own, and a package-level re-export would make `import oncall.collectors`
pull in the Kubernetes client, the Prometheus client and everything after them,
however little of it the caller wanted.

The binding that does need one home — source to collector to cadence — lives in
registry.py, which imports everything by design and is the only module that should.
"""
