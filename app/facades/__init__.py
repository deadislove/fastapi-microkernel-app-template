# Import every facade's router module here so its self-registration with
# `facade_registry` (see app/facades/registry.py) runs at import time.
# main.py imports `facade_registry` (which imports this package first) and
# mounts whatever `facade_registry.routers()` returns — it never needs to
# import a specific facade module by name, the same "add a module, don't
# touch the composition root" story plugins already have.
#
# Adding a new facade with an HTTP surface: add its router import below.
import app.facades.router  # noqa: F401
