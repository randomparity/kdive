"""One module per provider, each exposing a ``BINDING`` the contract registry discovers.

A provider joins the shared suites by adding a module here. Nothing in
``tests/providers/contract/`` needs to change to admit one, which is the property the
remote-libvirt sibling (#2200) depends on.
"""
