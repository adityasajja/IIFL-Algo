"""Application services.

The layer between the API (transport) and the packages that own state. A route
may not touch SQL or a broker directly; it calls a service, so authorization and
audit happen in exactly one place per action.
"""
