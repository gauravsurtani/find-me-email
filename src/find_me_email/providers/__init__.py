from find_me_email.providers.apify_b2b_leads import ApifyB2BLeadsProvider
from find_me_email.providers.apify_broad_serp import ApifyBroadSerpProvider
from find_me_email.providers.apify_harvestapi import ApifyHarvestAPIProvider
from find_me_email.providers.apify_school_serp import ApifySchoolSerpProvider
from find_me_email.providers.base import EnrichmentProvider
from find_me_email.providers.college_pattern_guess import CollegePatternGuessProvider
from find_me_email.providers.github_email_leak import GithubEmailLeakProvider
from find_me_email.providers.stubs.exa import ExaProvider

REGISTRY: dict[str, type[EnrichmentProvider]] = {
    "apify_b2b_leads": ApifyB2BLeadsProvider,
    "apify_broad_serp": ApifyBroadSerpProvider,
    "apify_harvestapi": ApifyHarvestAPIProvider,
    "apify_school_serp": ApifySchoolSerpProvider,
    "college_pattern_guess": CollegePatternGuessProvider,
    "exa": ExaProvider,
    "github_email_leak": GithubEmailLeakProvider,
}


def build_provider(name: str, config: dict) -> EnrichmentProvider:
    if name not in REGISTRY:
        raise KeyError(f"Unknown provider: {name}. Registered: {list(REGISTRY)}")
    return REGISTRY[name](config)


__all__ = ["EnrichmentProvider", "REGISTRY", "build_provider"]
