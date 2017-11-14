# -*- coding: utf-8 -*-
# Generated by Django 1.11.2 on 2017-11-02 15:39
from __future__ import unicode_literals, print_function

import json
import time

from array import array
from datetime import timedelta
from django.core.cache import cache
from django.db import migrations
from django.db.models import Prefetch
from django.utils import timezone
from temba.utils import chunk_list

# these are called out here because we can't reference the real FlowRun in this migration
PATH_NODE_UUID = 'node_uuid'
PATH_ARRIVED_ON = 'arrived_on'
PATH_EXIT_UUID = 'exit_uuid'
PATH_MAX_STEPS = 100


def backfill_flowrun_path(ActionSet, FlowRun, FlowStep):
    # get all flow action sets
    action_sets = list(ActionSet.objects.all())
    if not action_sets:
        return

    print("Found %d flow action sets..." % len(action_sets))

    # make map of action set node UUIDs to their exit UUIDs
    action_set_uuid_to_exit = {a.uuid: a.exit_uuid for a in action_sets if a.exit_uuid}

    if len(action_sets) != len(action_set_uuid_to_exit):
        raise ValueError(
            "Found actionsets without exit_uuids, use ensure_current_version command to migrate these flows forward"
        )

    # has this migration been run before but didn't complete?
    highpoint = cache.get('path_mig_highpoint')

    # get all flow run ids we're going to migrate
    run_ids = FlowRun.objects.filter(flow__is_active=True).values_list('id', flat=True).order_by('id')

    if highpoint:
        print("Resuming from previous highpoint at run #%d" % int(highpoint))
        run_ids = run_ids.filter(id__gt=int(highpoint))

    print("Fetching runs that need to be migrated (hold tight)...")

    run_ids = array(str('l'), run_ids)

    print("Found %d runs that need to be migrated" % len(run_ids))

    num_updated = 0
    num_trimmed = 0
    start = time.time()

    # we want to prefetch steps with each flow run, in chronological order
    steps_prefetch = Prefetch('steps', queryset=FlowStep.objects.only('step_uuid', 'step_type', 'rule_uuid', 'arrived_on').order_by('arrived_on'))

    for id_batch in chunk_list(run_ids, 1000):
        batch = FlowRun.objects.filter(id__in=id_batch).order_by('id').prefetch_related(steps_prefetch)

        for run in batch:
            path = []
            for step in run.steps.all():
                step_dict = {PATH_NODE_UUID: step.step_uuid, PATH_ARRIVED_ON: step.arrived_on.isoformat()}
                if step.step_type == 'R':
                    step_dict[PATH_EXIT_UUID] = step.rule_uuid
                else:
                    exit_uuid = action_set_uuid_to_exit.get(step.step_uuid)
                    if exit_uuid:
                        step_dict[PATH_EXIT_UUID] = exit_uuid

                path.append(step_dict)

            # trim path if necessary
            if len(path) > PATH_MAX_STEPS:
                path = path[len(path) - PATH_MAX_STEPS:]
                num_trimmed += 1

            run.path = json.dumps(path)
            run.save(update_fields=('path',))

            cache.set('path_mig_highpoint', str(run.id), 60 * 60 * 24 * 7)

        num_updated += len(batch)
        updated_per_sec = num_updated / (time.time() - start)

        # figure out estimated time remaining
        time_remaining = ((len(run_ids) - num_updated) / updated_per_sec)
        finishes = timezone.now() + timedelta(seconds=time_remaining)

        print("Updated %d runs of %d (%2.2f per sec) Est finish: %s" % (num_updated, len(run_ids), updated_per_sec, finishes))

    print("Run path migration completed in %d mins. %d paths were trimmed" % ((int(time.time() - start) / 60), num_trimmed))


def apply_manual():
    from temba.flows.models import ActionSet, FlowRun, FlowStep
    backfill_flowrun_path(ActionSet, FlowRun, FlowStep)


def apply_as_migration(apps, schema_editor):
    ActionSet = apps.get_model('flows', 'ActionSet')
    FlowRun = apps.get_model('flows', 'FlowRun')
    FlowStep = apps.get_model('flows', 'FlowStep')
    backfill_flowrun_path(ActionSet, FlowRun, FlowStep)


class Migration(migrations.Migration):

    dependencies = [
        ('flows', '0126_flowrun_path'),
    ]

    operations = [
        migrations.RunPython(apply_as_migration)
    ]
