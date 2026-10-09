# A failed blob delivery is classified as a CODE BUG and terminally kills the task

**Measured 2026-08-06 on instance `40000054` (`runq_a4833d0e…`, RTX 3060 Ti, 11 slots).
FOUR tasks across TWO campaigns and TWO blobs, in two hours.**

    01:15:45  ship_failed   code blob f010d788… could not be delivered
    01:18:55  task_failed   m49_sero_repair2/S2_dose      "ZERO PROGRESS (likely code/config bug, not infra)"
    01:19:02  task_failed   m49_sero_repair2/S1_mod       same
    03:05:05  ship_failed   code blob ae90cf2d… could not be delivered
    03:08:21  task_failed   canonintl_ship_n6/fintl_s1    same
    03:11:56  task_failed   canonintl_ship_n6/fintl_s1b   same

Each died within **7–8 seconds** of `start`, before any training. The box is NOT broken — 2 done and 5
running throughout — so this is not a bad-box story; it is a bad-DELIVERY story on a healthy box.

## Why it is a defect, not just bad luck

1. **The label is exactly backwards.** `dispatcher.py:4453` sets
   `"ZERO PROGRESS (likely code/config bug, not infra)"` whenever a failed task wrote no checkpoint or
   TB event. That tripwire is right in general (it exists because a device-mismatch bug once crashed
   every arm of a campaign identically). It is wrong here: the code never arrived.
2. **`task_failed` is TERMINAL.** `registry_db.LEGAL_TRANSITIONS` gives it no auto-requeue, by design
   ("your code is broken, stop shipping it"). So a transient delivery failure **permanently burns the
   lane**, where an `infra_failed` would retry elsewhere at no retry cost.
3. **The discriminator ALREADY EXISTS in the same file.** The undeliverable-box remediation
   (`dispatcher.py:3416`) uses precisely *"we have a `ship_failed` event for THIS task on THIS
   instance — the discriminator that separates 'undeliverable' from 'merely queued behind a slow
   pass'"*. The failure classifier two thousand lines away never consults it.
4. **The existing guard cannot fire here**, for two independent reasons, both deliberate:
   * it requires the box to have **no `running`/`preempting` occupant** ("never touch a box doing real
     work") — and 40000054 had five running tasks from other campaigns; and
   * it requires the task to be stuck in `claimed` past `ship_timeout_min` — but these tasks were not
     stuck. Delivery failed, the dispatcher **shipped anyway**, and they died fast.

   So the guard covers "undeliverable AND idle" and leaves "undeliverable AND busy" completely
   uncovered — which is the case that actually happened, four times.

## The fix (NOT applied here — it is daemon code)

At the zero-progress classification site, consult the same discriminator: if the task has a
`ship_failed` event on the instance it just died on, classify **INFRA** (retryable) rather than
`task_failed`. Optionally also refuse to `ship` after a `ship_failed` for the same (task, instance)
without a re-verify.

⚠ **This changes `fleet/dispatcher.py`, so it is a silent no-op until `make dispatch-restart`.**
It is deliberately NOT applied in this commit: the fleet currently has ~20 tasks running across
several sessions, and restarting the singleton coordinator is a blast-radius decision for the fleet
owner, not a side effect of one campaign's write-up.

## What to do meanwhile

**Read `runq show <id>` before believing a `ZERO PROGRESS` label.** A `ship_failed` one event earlier
means the code never landed. The cheap confirmation is the sibling test: if other cells of the same
campaign, on the same config and the same blob, are running fine, it is not a code bug — 11 of 12 were,
here.

---

## ⚠ Correction to this file's own commit message (546b7963)

That commit's message is CORRUPTED and should not be read as written. I wrote it with
`git commit -m "…"` containing backticks, and the shell EXECUTED them inside the double quotes:

* `` `claimed` `` and `` `runq show` `` were replaced by the (empty) output of running them, so the
  message reads "task to be stuck in ," with words missing;
* `` `make dispatch-restart` `` **actually ran**, restarting the singleton fleet coordinator — the
  precise action this document had just argued should be the fleet owner's call, not a side effect of
  a write-up.

**Verified afterwards: no damage.** The coordinator came back on the same sha (`0133fe29`, "Already
up to date"), so no unreviewed code was deployed; 32 tasks were running fleet-wide immediately after,
all 11 live cells of `canonintl_ship_n6` kept checkpointing, and the only failure in the window was
`fintl_s1b`, which had died three minutes BEFORE the restart. The outcome was luck, not care.

The commit cannot be amended — this repo does not force-push — so the record is corrected here.

**RULE, and it is mechanical:** write commit messages with `git commit -F -` and a QUOTED heredoc
(`<<'EOF'`). Never `-m "…"` when the text contains backticks. Every other commit in this campaign used
the safe form, which is why this surfaced only once.
