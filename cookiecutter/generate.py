#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
cookiecutter.generate
---------------------

Functions for generating a project from a project template.
"""

from __future__ import unicode_literals

import fnmatch
import io
import logging
import os
import shutil

from jinja2 import FileSystemLoader, Template
from jinja2.environment import Environment
from jinja2.exceptions import TemplateSyntaxError
from binaryornot.check import is_binary

from .exceptions import NonTemplatedInputDirException
from .find import find_template
from .utils import make_sure_path_exists, work_in
from .hooks import run_hook
from .parsers import load_context_from_file


def copy_without_render(path, context):
    """
    Returns True if `path` matches some pattern in the
    `_copy_without_render` context setting.

    :param path: A file-system path referring to a file or dir that
        should be rendered or just copied.
    :param context: cookiecutter context.
    """
    try:
        for dont_render in context['cookiecutter']['_copy_without_render']:
            if fnmatch.fnmatch(path, dont_render):
                return True
    except KeyError:
        return False

    return False


def generate_context(context_file='cookiecutter.json', default_context=None,
                     extra_context=None):
    """
    Generates the context for a Cookiecutter project template.
    Loads the JSON file as a Python object, with key being the JSON filename.

    :param context_file: JSON file containing key/value pairs for populating
        the cookiecutter's variables.
    :param default_context: Dictionary containing config to take into account.
    :param extra_context: Dictionary containing configuration overrides
    """

    context = load_context_from_file(context_file)

    # Overwrite context variable defaults with the default context from the
    # user's global config, if available
    if default_context:
        context.update(default_context)
    if extra_context:
        context.update(extra_context)

    logging.debug('Context generated is {0}'.format(context))
    return context


def generate_file(project_dir, infile, context, env):
    """
    1. Render the filename of infile as the name of outfile.
    2. Deal with infile appropriately:

        a. If infile is a binary file, copy it over without rendering.
        b. If infile is a text file, render its contents and write the
           rendered infile to outfile.

    Precondition:

        When calling `generate_file()`, the root template dir must be the
        current working directory. Using `utils.work_in()` is the recommended
        way to perform this directory change.

    :param project_dir: Absolute path to the resulting generated project.
    :param infile: Input file to generate the file from. Relative to the root
        template dir.
    :param context: Dict for populating the cookiecutter's variables.
    :param env: Jinja2 template execution environment.
    """

    logging.debug('Generating file {0}'.format(infile))

    # Render the path to the output file (not including the root project dir)
    outfile_tmpl = Template(infile)
    outfile = os.path.join(project_dir, outfile_tmpl.render(**context))
    logging.debug('outfile is {0}'.format(outfile))

    # Just copy over binary files. Don't render.
    logging.debug("Check {0} to see if it's a binary".format(infile))
    if is_binary(infile):
        logging.debug('Copying binary {0} to {1} without rendering'
                      .format(infile, outfile))
        shutil.copyfile(infile, outfile)
    else:
        # Force fwd slashes on Windows for get_template
        # This is a by-design Jinja issue
        infile_fwd_slashes = infile.replace(os.path.sep, '/')

        # Render the file
        try:
            tmpl = env.get_template(infile_fwd_slashes)
        except TemplateSyntaxError as exception:
            # Disable translated so that printed exception contains verbose
            # information about syntax error location
            exception.translated = False
            raise
        rendered_file = tmpl.render(**context)

        logging.debug('Writing {0}'.format(outfile))

        with io.open(outfile, 'w', encoding='utf-8') as fh:
            fh.write(rendered_file)

    # Apply file permissions to output file
    shutil.copymode(infile, outfile)


def render_and_create_dir(dirname, context, output_dir):
    """
    Renders the name of a directory, creates the directory, and
    returns its path.
    """

    name_tmpl = Template(dirname)
    rendered_dirname = name_tmpl.render(**context)
    logging.debug('Rendered dir {0} must exist in output_dir {1}'.format(
        rendered_dirname,
        output_dir
    ))
    dir_to_create = os.path.normpath(
        os.path.join(output_dir, rendered_dirname)
    )
    make_sure_path_exists(dir_to_create)
    return dir_to_create


def ensure_dir_is_templated(dirname):
    """
    Ensures that dirname is a templated directory name.
    """
    if '{{' in dirname and '}}' in dirname:
        return True
    else:
        raise NonTemplatedInputDirException


def generate_files(repo_dir, context=None, output_dir='.'):
    """
    Renders the templates and saves them to files.

    :param repo_dir: Project template input directory.
    :param context: Dict for populating the template's variables.
    :param output_dir: Where to output the generated project dir into.
    """

    template_dir = find_template(repo_dir)
    logging.debug('Generating project from {0}...'.format(template_dir))
    context = context or {}

    unrendered_dir = os.path.split(template_dir)[1]
    ensure_dir_is_templated(unrendered_dir)
    if 'cookiecutter' not in context:
        context['cookiecutter'] = context  # compatibility
    project_dir = render_and_create_dir(unrendered_dir, context, output_dir)

    # We want the Jinja path and the OS paths to match. Consequently, we'll:
    #   + CD to the template folder
    #   + Set Jinja's path to '.'
    #
    #  In order to build our files to the correct folder(s), we'll use an
    # absolute path for the target folder (project_dir)

    project_dir = os.path.abspath(project_dir)
    logging.debug('project_dir is {0}'.format(project_dir))

    # run pre-gen hook from repo_dir
    with work_in(repo_dir):
        run_hook('pre_gen_project', project_dir, context)

    with work_in(template_dir):
        env = Environment(keep_trailing_newline=True)
        env.loader = FileSystemLoader('.')

        for root, dirs, files in os.walk('.'):
            # We must separate the two types of dirs into different lists.
            # The reason is that we don't want ``os.walk`` to go through the
            # unrendered directories, since they will just be copied.
            copy_dirs = []
            render_dirs = []

            for d in dirs:
                d_ = os.path.normpath(os.path.join(root, d))
                # We check the full path, because that's how it can be
                # specified in the ``_copy_without_render`` setting, but
                # we store just the dir name
                if copy_without_render(d_, context):
                    copy_dirs.append(d)
                else:
                    render_dirs.append(d)

            for copy_dir in copy_dirs:
                indir = os.path.normpath(os.path.join(root, copy_dir))
                outdir = os.path.normpath(os.path.join(project_dir, indir))
                logging.debug(
                    'Copying dir {0} to {1} without rendering'
                    ''.format(indir, outdir)
                )
                shutil.copytree(indir, outdir)

            # We mutate ``dirs``, because we only want to go through these dirs
            # recursively
            dirs[:] = render_dirs
            for d in dirs:
                unrendered_dir = os.path.join(project_dir, root, d)
                render_and_create_dir(unrendered_dir, context, output_dir)

            for f in files:
                infile = os.path.normpath(os.path.join(root, f))
                if copy_without_render(infile, context):
                    outfile_tmpl = Template(infile)
                    outfile_rendered = outfile_tmpl.render(**context)
                    outfile = os.path.join(project_dir, outfile_rendered)
                    logging.debug(
                        'Copying file {0} to {1} without rendering'
                        ''.format(infile, outfile)
                    )
                    shutil.copyfile(infile, outfile)
                    shutil.copymode(infile, outfile)
                    continue
                logging.debug('f is {0}'.format(f))
                generate_file(project_dir, infile, context, env)

    # run post-gen hook from repo_dir
    with work_in(repo_dir):
        run_hook('post_gen_project', project_dir, context)
