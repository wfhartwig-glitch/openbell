"""Specialist desks: small deterministic modules, each fetching one kind of fact and producing one
section of the brief. No AI agents, no LLM calls, no paid services, no API keys.

A desk module defines:
    NAME     short lowercase id, used in the log tag [DESK-<name>]
    PRIVATE  True only for a desk that may use the owner's own data (none do today)
    fetch()  -> facts dict; every fact carries its source and timestamp
    build_section(facts, mode)  -> HTML string, or None when there is nothing to show
Run them with desks.framework.
"""
