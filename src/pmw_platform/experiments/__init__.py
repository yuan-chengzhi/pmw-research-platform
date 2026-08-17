"""Optional experiment plugins.

Nothing under this package is part of the generic session runtime.  Modules
here define record schemas, pure validators and analysis helpers for specific
experimental treatments.  The runtime core does not import them, and importing
one never starts a session, a model request or a network call.
"""
