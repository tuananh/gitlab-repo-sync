#!/usr/bin/env python3

# usage: ./sync.py \
#   --gitlab-src-url https://gitlab-source.com \
#   --gitlab-dest-url https://gitlab-destination.com \
#   --src-token <token> \
#   --dest-token <token> \
#   --groupid 4 \
#   --ignore-project-paths dso/proj1,dso/proj2

import argparse
import sys
import os
import tempfile
import logging
from pathlib import Path
import multiprocessing as mp
from multiprocessing import Pool
import gitlab
import git

logging.basicConfig(
    format='%(asctime)s %(module)s %(filename)s:%(lineno)s - %(message)s')
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def main(arguments):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawTextHelpFormatter)

    parser.add_argument('--gitlab-src-url', type=str, required=True,
                        help="gitlab source url. eg: https://gitlab-source.com ")
    parser.add_argument('--gitlab-dest-url', type=str, required=True,
                        help="gitlab destination url. eg: https://gitlab-destination.com ")
    parser.add_argument('--src-token', type=str,
                        required=True, help="gitlab source token")
    parser.add_argument('--dest-token', type=str,
                        required=True, help="gitlab dest token")
    parser.add_argument('--groupid', type=str, required=True, help="group id")
    parser.add_argument('--cachedir', type=str,
                        help="cache dir. default to a dir in /tmp")
    parser.add_argument('--ignore-project-paths', type=str,
                        help="ignore project paths. Eg: dso/app1,dso/app2. default is empty(sync all)")

    args = parser.parse_args(arguments)

    src_url = args.gitlab_src_url
    dest_url = args.gitlab_dest_url
    group_id = args.groupid
    src_token = args.src_token
    dest_token = args.dest_token
    ignore_project_paths = args.ignore_project_paths.split(
        ',') if args.ignore_project_paths is not None else []

    src_gl = gitlab.Gitlab(url=src_url, private_token=src_token)
    dest_gl = gitlab.Gitlab(url=dest_url, private_token=dest_token)
    src_gl.auth()

    group = src_gl.groups.get(group_id, lazy=True)
    src_projects = group.projects.list(as_list=False, include_subgroups=True)

    logger.debug('# of projects: %d found in source', len(src_projects))

    cachedir = args.cachedir
    if args.cachedir is None:
        cachedir = tempfile.TemporaryDirectory()

    logger.debug('using temporary directory %s', cachedir)

    # First we sync the group structure, and then sync the project structure
    mirror_group_structure(src_gl, dest_gl, group_id)
    mirror_project_structure(src_gl, dest_gl, group_id, ignore_project_paths)

    pool = Pool(mp.cpu_count())
    sync_repo_params = []
    for src_project in src_projects:
        if (src_project.path_with_namespace not in ignore_project_paths):
            sync_repo_params.append((src_project, cachedir, args))
        else:
            logger.debug('skip sync for project %s',
                         src_project.path_with_namespace)

    pool.map(sync_repo, sync_repo_params)
    pool.close()


def sync_repo(params):
    (src_project, cachedir, args) = params
    logger.debug('sync started %s', src_project.path_with_namespace)
    try:
        src_repo_url = src_project.http_url_to_repo
        path_with_namespace = src_project.path_with_namespace

        clone_path = os.path.join(cachedir, path_with_namespace)
        Path(clone_path).mkdir(parents=True, exist_ok=True)

        if is_git_repo(clone_path):
            logger.debug('found in cachedir. pull and push %s',
                         src_project.path_with_namespace)
            cloned_repo = git.Repo(clone_path)
            cloned_repo.remotes.origin.pull()
            cloned_repo.remotes.origin.fetch(tags=True)
            # git push mirror --tags "refs/remotes/origin/*:refs/heads/*"
            cloned_repo.git.push('mirror', '--tags',
                                 'refs/remotes/origin/*:refs/heads/*')
        else:
            url_with_cred = add_token_to_url(src_repo_url, args.src_token)
            cloned_repo = git.Repo.clone_from(url_with_cred, clone_path)
            mirror_url = add_token_to_url(
                f'{args.gitlab_dest_url}/{path_with_namespace}.git', args.dest_token)
            cloned_repo.create_remote('mirror', url=mirror_url)
            cloned_repo.git.push('mirror', '--tags',
                                 'refs/remotes/origin/*:refs/heads/*')

        logger.debug('sync completed %s', src_project.path_with_namespace)

    except Exception as e:
        logger.error("sync error on %s: %s",
                     src_project.path_with_namespace, e)


def is_git_repo(path):
    try:
        _ = git.Repo(path).git_dir
        return True
    except git.exc.InvalidGitRepositoryError:
        return False


def get_project_by_path(gl_client, full_path):
    try:
        return gl_client.projects.get(full_path)
    except Exception as e:
        return None


def get_group_by_path(gl_client, full_path):
    """
    Find group by full_path, return None if not found
    """
    try:
        return gl_client.groups.get(full_path)
    except Exception as e:
        return None


def create_group_structure_by_path(dest_client, src_client, path):
    # from full path, get parent group path: 'a/b/c/d' => 'a/b/c'
    parts = path.split('/')
    parent_path = '/'.join(parts[0:len(parts)-1])
    src_group = src_client.groups.get(path)

    if path:
        if (not parent_path and len(parts) == 1):
            # case top-level-group/
            try:
                dest_client.groups.get(path)
            except Exception as e:
                logger.debug('creating top level group')
                group = dest_client.groups.create(
                    {'name': src_group.name, 'description': f'Mirrored from {src_group.web_url}', 'path': path, 'parent_id': None})
        else:
            # case multi-level path
            logger.debug('path %s, parent path %s', path, parent_path)
            parent_group = get_group_by_path(dest_client, parent_path)
            if not parent_group:
                logger.debug(
                    'parent group (%s) not found. Recursively creating...', parent_path)
                create_group_structure_by_path(
                    dest_client, src_client, parent_path)
                create_group_structure_by_path(dest_client, src_client, path)
            else:
                group_name = parts[-1]
                group = dest_client.groups.create(
                    {'name': src_group.name, 'path': group_name, 'parent_id': parent_group.id})
                logger.debug(
                    'creating new group [%s], id=%s under %s', group_name, group.id, parent_group.full_path)
    else:
        logger.debug('empty path => skipping')


def mirror_project_structure(src_client, dest_client, group_id, ignore_project_paths):
    """
    Mirror all the projects, assuming groups are already created.
    """
    group = src_client.groups.get(group_id, lazy=True)
    projects = group.projects.list(as_list=False, include_subgroups=True)
    logger.debug('found %d projects from source gitlab', len(projects))

    for src_project in projects:
        full_path = src_project.path_with_namespace
        if (full_path not in ignore_project_paths):
            group_path = "/".join(list(full_path.split('/')[0:-1]))
            group = get_group_by_path(dest_client, group_path)

            remote_project = get_project_by_path(dest_client, full_path)
            if (not remote_project):
                remote_project = dest_client.projects.create(
                    {'name': src_project.name, 'description': f'Mirrored from {src_project.http_url_to_repo}', 'namespace_id': group.id})
                logger.debug('remote project %s created',
                             remote_project.path_with_namespace)
            else:
                logger.debug('remote project %s exists. SKIP creating.',
                             remote_project.path_with_namespace)
        else:
            logger.debug('skip creating project %s', full_path)


def mirror_group_structure(src_client, dest_client, group_id):
    src_group = src_client.groups.get(group_id)
    descendant_groups = src_group.descendant_groups.list()
    logger.debug('found %d sub-groups', len(descendant_groups))

    for subgroup in descendant_groups:
        path = subgroup.full_path
        try:
            group = get_group_by_path(dest_client, path)
            orig_group = get_group_by_path(src_client, path)
            if (group is not None):
                logger.debug(
                    'group [%s] FOUND in remote. SKIP creating.', path)
            else:
                logger.debug('group [%s] NOT FOUND. Creating ...', path)
                create_group_structure_by_path(dest_client, src_client, path)

        except Exception as e:
            logger.debug('unexpected error: path=%s: %s', path, e)


def add_token_to_url(url, token):
    return url.replace('https://', 'https://dummy:' + token+'@')


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
