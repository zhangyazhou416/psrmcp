"""MCP prompts (DESIGN.md 5.2): workflow guides, never hardcoded CLI commands."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import invalid_input, not_found

from mcp.types import Prompt, PromptArgument, PromptMessage, TextContent


@dataclass(frozen=True)
class PromptDef:
    description: str
    arguments: tuple[tuple[str, str, bool], ...]  # (name, description, required)
    template: str  # .format(**kwargs) applied to user-supplied values


_DEFS: dict[str, PromptDef] = {
    "reduce_observation": PromptDef(
        description="Guide through a single-observation reduction: inspect -> dedisperse -> RFI -> standard profile -> measure",
        arguments=(
            ("archive", "Path or artifact ID of the input archive", True),
            ("pulsar_name", "Pulsar name (J name), used to fetch catalog parameters", False),
            ("dm", "Known dispersion measure in pc cm^-3; inferred from data header/catalog if omitted", False),
            ("rfi_policy", "RFI policy: conservative|aggressive|skip, default conservative", False),
        ),
        template=(
            "Please perform a full pulsar reduction on the observation {archive}.\n"
            "{pulsar_name_line}{dm_line}"
            "RFI policy: {rfi_policy}.\n\n"
            "Requirements:\n"
            "1. First confirm backend availability with the environment and data discovery tools, then inspect the data metadata.\n"
            "2. Before dedispersing, confirm the DM source (data header / catalog / user-supplied); ask when unsure.\n"
            "3. For RFI, scan first then clean; under the conservative policy prefer zapping fewer channels and record the mask.\n"
            "4. Generate the standard profile and measure S/N and width; reference all products by artifact ID.\n"
            "5. For long tasks use execution_mode=auto and poll with job_get; on failure read the log artifact first.\n"
            "6. Report the key numbers from structured results at each step; never invent unmeasured values."
        ),
    ),
    "timing_analysis": PromptDef(
        description="Guide through par+tim timing fits, residual checks, and parameter interpretation",
        arguments=(
            ("par", "Path or artifact ID of the initial .par file", True),
            ("tim", "Path or artifact ID of the TOA .tim file", True),
            ("backend", "Fit backend: pint (default) or tempo2", False),
        ),
        template=(
            "Please run a timing analysis on par={par} and tim={tim} (backend: {backend}).\n\n"
            "Requirements:\n"
            "1. Fit first; report n_toas, chi2/dof, RMS/WRMS, and the fitted parameters with uncertainties.\n"
            "2. Then diagnose: outliers, JUMPs, DM information, residual distribution; watch for anomalous reduced chi2.\n"
            "3. Clean the data following the diagnostics recommendations and refit, comparing RMS before and after.\n"
            "4. When interpreting parameters, give physical reasoning (spin/proper motion/binary) and note which parameters are held fixed without fitting.\n"
            "5. Reference all intermediate products (par/residuals/plot) by artifact ID."
        ),
    ),
    "diagnose_rfi": PromptDef(
        description="Guide through bad-channel analysis and zap strategy selection",
        arguments=(("archive", "Path or artifact ID of the archive to analyze", True),),
        template=(
            "Please analyze the radio-frequency interference in {archive}.\n\n"
            "Requirements:\n"
            "1. Run an RFI scan first; report the bad-channel fraction and a suggested mask (with plot).\n"
            "2. Discuss astronomical plausibility: broadband bursts, persistent narrowband signals, on-pulse vs off-pulse differences.\n"
            "3. Give zap recommendations (which channels and why); trial-zap a small range first to verify the impact.\n"
            "4. After cleaning, re-assess profile quality (S/N change) and honestly report where the statistics are limiting."
        ),
    ),
    "catalog_to_ephemeris": PromptDef(
        description="Fetch parameters from the catalog and export a .par file",
        arguments=(
            ("name", "Pulsar name", True),
            ("format", "par format: tempo1 (default) or tempo2", False),
        ),
        template=(
            "Please export a {format} ephemeris file for pulsar {name}.\n\n"
            "Requirements:\n"
            "1. Query the catalog for parameters and report key values (RA/Dec/DM/period).\n"
            "2. Export the .par and check the parameters it contains; if binary parameters are missing, advise on handling the W_BINARY_PARAMS_INCOMPLETE warning.\n"
            "3. State which downstream processing the par suits (dedispersion/timing) and its limitations (no proper motion, etc.)."
        ),
    ),
    "compare_timing_models": PromptDef(
        description="Fit the same par+tim with PINT and tempo2 separately and compare the results (later phase)",
        arguments=(
            ("par", "Timing model .par", True),
            ("tim", "TOA .tim", True),
        ),
        template=(
            "Please fit par={par} and tim={tim} with both PINT and tempo2, and compare the results.\n\n"
            "Requirements:\n"
            "1. Produce fit statistics and residuals from each backend; tempo2 output parsing is still a TODO (contract) — report failures as they are.\n"
            "2. Comparison table: RMS/WRMS, reduced chi2, key parameter values and uncertainty differences.\n"
            "3. Discuss the sources of differences (time ephemerides, solar-system ephemeris, weighting) and recommend a backend with reasons."
        ),
    ),
}


def list_prompts() -> list[Prompt]:
    return [
        Prompt(
            name=name,
            description=d.description,
            arguments=[
                PromptArgument(name=n, description=desc, required=req)
                for n, desc, req in d.arguments
            ],
        )
        for name, d in _DEFS.items()
    ]


def get_prompt(name: str, arguments: dict[str, str] | None) -> list[PromptMessage]:
    d = _DEFS.get(name)
    if d is None:
        raise not_found(f"unknown prompt: {name} (available: {', '.join(sorted(_DEFS))})")
    supplied = arguments or {}
    kwargs: dict[str, str] = {}
    for arg_name, _desc, required in d.arguments:
        val = str(supplied.get(arg_name, "")).strip()
        if not val:
            if required:
                raise invalid_input(f"prompt {name} missing required argument: {arg_name}")
            kwargs[arg_name] = "(not provided)"
        else:
            kwargs[arg_name] = val
    # Optional-context lines for reduce_observation; empty when unsupplied.
    kwargs["pulsar_name_line"] = (
        "" if kwargs.get("pulsar_name") == "(not provided)"
        else f"Pulsar: {kwargs['pulsar_name']}.\n"
    )
    kwargs["dm_line"] = (
        "" if kwargs.get("dm") == "(not provided)" else f"DM: {kwargs['dm']} pc cm^-3.\n"
    )
    text = d.template.format(**kwargs)
    return [PromptMessage(role="user", content=TextContent(type="text", text=text))]


__all__ = ["list_prompts", "get_prompt"]
