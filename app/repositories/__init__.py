"""
repositories — The only package that talks to MongoDB.

Each module wraps one collection. Repositories store and fetch documents; they
make no business decisions (that is the services' job) and raise no HTTP
errors (that is the routers' job).
"""
