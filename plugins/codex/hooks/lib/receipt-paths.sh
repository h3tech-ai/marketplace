#!/usr/bin/env bash
# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
#
# THE receipt sweep for shell hooks. One definition, sourced by every hook that
# needs to see receipts on disk.
#
# `story_pipeline.receipts_dir_for` resolves the ONE canonical directory and is
# what the gates read. This is the other question: "every receipt anywhere in
# this project", which is what a hook that ships, counts, or displays needs. A
# session can straddle a layout migration, and an earlier Cycle's receipts still
# exist, so the sweep globs rather than resolving.
#
# It exists because there were SIX independent copies of this glob across the
# hooks, and the SPQ layout was missing from every one of them: receipts shipped
# from no host (#326) and every SPQ session reported `0 receipts` (#336).
# `receipts_dir_for`'s own docstring names this failure mode:
#
#     Five independent derivations of one path (add the three shell hooks) is
#     how that happens
#
# Three layouts, and SPQ now has exactly ONE receipt home (ADR-035): `receipts/`
# sits directly under the Cycle (`spq_paths.receipts_dir`), where three homes at
# two different depths used to answer to the retired ownership objects.
#
#   receipts/                        flat
#   specs/<active>/receipts/         multi-spec (scrum/kanban)
#   spq/cycles/<cycle-id>/receipts/  SPQ
#
# The SPQ line stays a `*/receipts/*.json` sweep under `spq/` rather than
# tightening to that one depth, and deliberately: receipts written before the
# layout change are immutable evidence a hook still has to count and ship, and
# `find` reaching them costs nothing. Widening the one pattern is how a single
# definition serves both depths; adding a second line for the old one is exactly
# how six copies came to exist.

# Every receipt JSON in the project. One path per line, unordered.
# $1 = the .orchestrator directory.
synaptory_receipt_files() {
  _orch_dir="$1"
  [ -n "$_orch_dir" ] || return 0
  {
    [ -d "$_orch_dir/receipts" ] \
      && find "$_orch_dir/receipts" -maxdepth 1 -name "*.json" -type f
    [ -d "$_orch_dir/specs" ] \
      && find "$_orch_dir/specs" -path "*/receipts/*.json" -type f
    [ -d "$_orch_dir/spq" ] \
      && find "$_orch_dir/spq" -path "*/receipts/*.json" -type f
  } 2>/dev/null
  # "No receipts" is a normal answer, not a failure. Without this the exit
  # status is the last `[ -d ... ]` test, so a project with no SPQ tree made
  # the function return 1 -- harmless today because no hook sets `-e`, and a
  # silent hook abort the day one does.
  return 0
}

# How many receipts exist. $1 = the .orchestrator directory.
synaptory_receipt_count() {
  synaptory_receipt_files "$1" | wc -l | tr -d ' '
}

# mtime as a unix timestamp, BSD (`-f %m`) and GNU (`-c %Y`) alike. Hooks run on
# a developer Mac and on a Linux CI runner, so neither spelling can be assumed.
_synaptory_mtime() {
  stat -f '%m' "$1" 2>/dev/null || stat -c '%Y' "$1" 2>/dev/null || echo 0
}

# The N newest receipts, newest first. $1 = .orchestrator dir, $2 = N (default 5).
#
# Deliberately NOT `synaptory_receipt_files ... | xargs ls -t`. With no receipts
# GNU xargs runs `ls -t` with no operands, which lists the CURRENT DIRECTORY and
# hands back an unrelated filename, while BSD xargs skips the command entirely.
# The same pipeline therefore returns a bogus "receipt" on CI and nothing on a
# Mac -- and a caller testing `[ -z "$RECENT" ]` to detect "no receipt" silently
# takes the wrong branch on one of the two.
synaptory_recent_receipts() {
  _orch_dir="$1"
  _want="${2:-5}"
  _files=$(synaptory_receipt_files "$_orch_dir")
  [ -n "$_files" ] || return 0
  printf '%s\n' "$_files" | while IFS= read -r _f; do
    [ -f "$_f" ] || continue
    printf '%s\t%s\n' "$(_synaptory_mtime "$_f")" "$_f"
  done | sort -rn | cut -f2- | head -n "$_want"
}
