# -*- coding: utf-8 -*-
# Generated by Django 1.9 on 2016-05-16 14:32
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('cbh_chembl_id_generator', '0007_remove_cbhplugin_output_json_path'),
    ]

    operations = [
        migrations.DeleteModel(
            name='CBHCompoundId',
        ),
        migrations.DeleteModel(
            name='CBHPlugin',
        ),
    ]
