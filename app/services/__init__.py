"""
services — All business rules live here.

Services take plain values (not HTTP requests), call repositories for data,
and raise DomainError subclasses (app/core/exceptions.py) when a rule says no.
They never import FastAPI or build MongoDB queries.
"""
