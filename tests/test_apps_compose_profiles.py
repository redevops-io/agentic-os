"""Static guard for deploy/apps/compose.yml's Compose `profiles:` scheme (the first use of profiles in this repo,
see the compose file's own "PROFILES" header comment). No Docker, no network: pure YAML-structure checks that
`docker compose --profile <agent> up -d` can only ever start that one agent's own services, never a neighbor's,
and that the always-on services (caddy, projects-console) stay valid regardless of which profiles are active.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "deploy" / "apps" / "compose.yml").read_text(encoding="utf-8"))
SERVICES = COMPOSE["services"]

ALWAYS_ON = {"caddy", "projects-console", "projects"}     # `projects` (the dashboards) came from main, always on

# profile name -> the service it's named after (three of the six services carry a "-agent" suffix the profile drops)
AGENT_SERVICE = {
    "revenue": "revenue-agent",
    "intelligence": "intelligence-agent",
    "content": "content-agent",
    "finance": "finance-agent",
    "customer-success": "customer-success",
    "security-compliance": "security-compliance",
}
AGENTS = set(AGENT_SERVICE)              # profile names
AGENT_SVC_NAMES = set(AGENT_SERVICE.values())   # the services those profiles belong to


def profiles_of(name: str) -> set[str]:
    return set(SERVICES[name].get("profiles") or [])


def depends_on_names(name: str) -> set[str]:
    dep = SERVICES[name].get("depends_on") or {}
    return set(dep) if isinstance(dep, dict) else set(dep)


def test_every_service_is_either_always_on_or_profiled_with_a_known_agent_name():
    for name, svc in SERVICES.items():
        profiles = set(svc.get("profiles") or [])
        if name in ALWAYS_ON:
            assert not profiles, f"{name} is meant to be always-on; remove its profiles key"
        else:
            assert profiles, f"{name} has no profiles key and is not in ALWAYS_ON - every other service must be profiled"
            assert profiles <= AGENTS, f"{name}: profile(s) {profiles - AGENTS} are not a known profile name"


def test_each_domain_agent_carries_exactly_its_own_profile():
    for profile, svc_name in AGENT_SERVICE.items():
        assert svc_name in SERVICES, f"no service named {svc_name!r} for profile {profile!r}"
        assert profiles_of(svc_name) == {profile}, f"{svc_name}: profiles must be exactly {{{profile!r}}}"


def test_every_agent_dependency_carries_a_profile_superset_of_the_agents_own():
    """Activating an agent's profile must always bring in everything that agent depends_on."""
    for profile, svc_name in AGENT_SERVICE.items():
        for dep in depends_on_names(svc_name):
            if dep in ALWAYS_ON:
                continue   # always-on dependencies need no profile check - they run regardless
            dep_profiles = profiles_of(dep)
            assert {profile} <= dep_profiles, (
                f"{svc_name} depends_on {dep!r}, but {dep!r}'s profiles {dep_profiles} do not cover "
                f"{svc_name}'s own profile {{{profile!r}}} - activating {profile} alone would not start {dep}"
            )


def test_oss_core_services_carry_no_profile_outside_the_dependents_that_need_them():
    """The inverse check: every profile an OSS-core service carries must belong to an agent that actually depends on it
    (directly, or via another profiled service in the same profile - e.g. twenty-db via twenty), so selecting one
    agent's profile never silently drags in a core nobody in that profile needs."""
    agent_deps: dict[str, set[str]] = {profile: depends_on_names(svc) for profile, svc in AGENT_SERVICE.items()}
    other_services = [s for s in SERVICES if s not in ALWAYS_ON and s not in AGENT_SVC_NAMES]
    for name in other_services:
        for p in profiles_of(name):
            depended_directly = name in agent_deps.get(p, set())
            depended_transitively = any(
                name in depends_on_names(other) and p in profiles_of(other)
                for other in other_services
                if other != name
            )
            assert depended_directly or depended_transitively, (
                f"{name}: carries profile {p!r} but no service in that profile depends on it (directly or transitively)"
            )


def test_projects_console_tolerates_any_subset_of_agent_profiles():
    """projects-console (always-on) must use the long-form depends_on with required: false for every profiled
    agent - the short list form makes `docker compose config` refuse the whole project unless every agent's
    profile is active (confirmed: this is a real `docker compose` validation error, not a style preference)."""
    dep = SERVICES["projects-console"]["depends_on"]
    assert isinstance(dep, dict), "projects-console must use the long-form depends_on mapping, not a plain list"
    for profile, svc_name in AGENT_SERVICE.items():
        assert svc_name in dep, f"projects-console must depend on {svc_name} (profile {profile!r})"
        assert dep[svc_name].get("required") is False, f"projects-console's dependency on {svc_name} must set required: false"


def test_no_seventh_profile_or_stray_agent_name_slipped_in():
    all_profiles: set[str] = set()
    for svc in SERVICES.values():
        all_profiles |= set(svc.get("profiles") or [])
    assert all_profiles == AGENTS, f"profiles in use {all_profiles} must equal exactly the 6 known agents {AGENTS}"
