"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import, unicode_literals

import os
import logging
from collections import OrderedDict

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from atomic_reactor.util import ImageName, chain_get


yaml = YAML()
log = logging.getLogger(__name__)


OPERATOR_CSV_KIND = "ClusterServiceVersion"


class NotOperatorCSV(Exception):
    """
    Data is not from a valid ClusterServiceVersion document
    """


class NamedPullspec(object):
    """
    Pullspec with a name and description
    """

    _image_key = "image"

    def __init__(self, data):
        """
        Initialize a NamedPullspec

        :param data: Dict-like object in JSON/YAML data
                     in which the name and image can be found
        """
        self.data = data

    @property
    def name(self):
        return self.data["name"]

    @property
    def image(self):
        return self.data[self._image_key]

    @image.setter
    def image(self, value):
        self.data[self._image_key] = value

    @property
    def description(self):
        raise NotImplementedError

    def as_yaml_object(self):
        """
        Convert pullspec to a {"name": <name>, "image": <image>} object

        :return: dict-like object compatible with ruamel.yaml
        """
        return CommentedMap([("name", self.name), ("image", self.image)])


class Container(NamedPullspec):
    @property
    def description(self):
        return "container {}".format(self.name)


class InitContainer(NamedPullspec):
    @property
    def description(self):
        return "initContainer {}".format(self.name)


class RelatedImage(NamedPullspec):
    @property
    def description(self):
        return "relatedImage {}".format(self.name)


class RelatedImageEnv(NamedPullspec):
    _image_key = "value"

    @property
    def name(self):
        # Construct name by removing prefix and converting to lowercase
        return self.data["name"][len("RELATED_IMAGE_"):].lower()

    @property
    def description(self):
        return "{} var".format(self.data["name"])


class Annotation(NamedPullspec):
    _image_key = NotImplemented

    @property
    def name(self):
        # Construct name by taking image repo and adding suffix
        return ImageName.parse(self.image).repo + "-annotation"

    @property
    def description(self):
        return "{} annotation".format(self._image_key)

    def with_key(self, image_key):
        self._image_key = image_key
        return self


class OperatorCSV(object):
    """
    A single ClusterServiceVersion file in an operator manifest.

    Can find and replace pullspecs for container images in predefined locations.
    """

    def __init__(self, path, data):
        """
        Initialize an OperatorCSV

        :param path: Path where data was found or where it should be written
        :param data: ClusterServiceVersion yaml data
        """
        if data.get("kind") != OPERATOR_CSV_KIND:
            raise NotOperatorCSV("Not a ClusterServiceVersion")
        self.path = path
        self.data = data

    @classmethod
    def from_file(cls, path):
        """
        Make an OperatorCSV from a file

        :param path: Path to file
        :return: OperatorCSV
        """
        with open(path) as f:
            data = yaml.load(f)
            return cls(path, data)

    def dump(self):
        """
        Write data to file (preserves comments)
        """
        with open(self.path, "w") as f:
            yaml.dump(self.data, f)

    def has_related_images(self):
        """
        Check if OperatorCSV has a non-empty relatedImages section.
        """
        return bool(self._related_image_pullspecs())

    def has_related_image_envs(self):
        """
        Check if OperatorCSV has any RELATED_IMAGE_* env vars.
        """
        return bool(self._related_image_env_pullspecs())

    def get_pullspecs(self):
        """
        Find pullspecs in predefined locations.

        :return: set of ImageName pullspecs
        """
        named_pullspecs = self._named_pullspecs()
        pullspecs = set()

        for p in named_pullspecs:
            image = ImageName.parse(p.image)
            log.debug("%s - Found pullspec for %s: %s", self.path, p.description, image)
            pullspecs.add(image)

        return pullspecs

    def replace_pullspecs(self, replacement_pullspecs):
        """
        Replace pullspecs in predefined locations.

        :param replacement_pullspecs: mapping of pullspec -> replacement
        """
        named_pullspecs = self._named_pullspecs()

        for p in named_pullspecs:
            old = ImageName.parse(p.image)
            new = replacement_pullspecs.get(old)

            if new is not None and old != new:
                log.debug("%s - Replaced pullspec for %s: %s -> %s",
                          self.path, p.description, old, new)
                p.image = new.to_str()  # `new` is an ImageName

    def replace_pullspecs_everywhere(self, replacement_pullspecs):
        """
        Replace all pullspecs found anywhere in data

        :param replacement_pullspecs: mapping of pullspec -> replacment
        """
        for k in self.data:
            self._replace_pullspecs_everywhere(self.data, k, replacement_pullspecs)

    def set_related_images(self):
        """
        Find pullspecs in predefined locations and put all of them in the
        .spec.relatedImages section (if it already exists, clear it first)
        """
        named_pullspecs = self._named_pullspecs()

        by_name = OrderedDict()
        conflicts = []

        for new in named_pullspecs:
            # Keep track only of the first instance with a given name.
            # Ideally, existing relatedImages should come first in the list,
            # otherwise error messages could be confusing.
            old = by_name.setdefault(new.name, new)
            # Check for potential conflict (same name, different image)
            if new.image != old.image:
                msg = ("{old.description}: {old.image} X {new.description}: {new.image}"
                       .format(old=old, new=new))
                conflicts.append(msg)

        if conflicts:
            raise RuntimeError("{} - Found conflicts when setting relatedImages:\n{}"
                               .format(self.path, "\n".join(conflicts)))

        related_images = (self.data.setdefault("spec", CommentedMap())
                                   .setdefault("relatedImages", CommentedSeq()))
        del related_images[:]

        for p in by_name.values():
            log.debug("%s - Set relatedImage %s (from %s): %s",
                      self.path, p.name, p.description, p.image)
            related_images.append(p.as_yaml_object())

    def _named_pullspecs(self):
        pullspecs = []
        # relatedImages should come first in the list
        pullspecs.extend(self._related_image_pullspecs())
        pullspecs.extend(self._annotation_pullspecs())
        pullspecs.extend(self._container_pullspecs())
        pullspecs.extend(self._init_container_pullspecs())
        pullspecs.extend(self._related_image_env_pullspecs())
        return pullspecs

    def _related_image_pullspecs(self):
        related_images_path = ("spec", "relatedImages")
        return [
            RelatedImage(r)
            for r in chain_get(self.data, related_images_path, default=[])
        ]

    def _deployments(self):
        deployments_path = ("spec", "install", "spec", "deployments")
        return chain_get(self.data, deployments_path, default=[])

    def _container_pullspecs(self):
        deployments = self._deployments()
        containers_path = ("spec", "template", "spec", "containers")
        return [
            Container(c)
            for d in deployments for c in chain_get(d, containers_path, default=[])
        ]

    def _annotation_pullspecs(self):
        annotations_path = ("metadata", "annotations")
        annotations = chain_get(self.data, annotations_path, default={})
        pullspecs = []
        if "containerImage" in annotations:
            pullspecs.append(Annotation(annotations).with_key("containerImage"))
        return pullspecs

    def _related_image_env_pullspecs(self):
        containers = self._container_pullspecs() + self._init_container_pullspecs()
        envs = [
            e for c in containers
            for e in c.data.get("env", []) if e["name"].startswith("RELATED_IMAGE_")
        ]
        for env in envs:
            if "valueFrom" in env:
                msg = '{}: "valueFrom" references are not supported'.format(env["name"])
                raise RuntimeError(msg)
        return [
            RelatedImageEnv(env) for env in envs
        ]

    def _init_container_pullspecs(self):
        deployments = self._deployments()
        init_containers_path = ("spec", "template", "spec", "initContainers")
        return [
            InitContainer(c)
            for d in deployments for c in chain_get(d, init_containers_path, default=[])
        ]

    def _replace_unnamed_pullspec(self, obj, key, replacement_pullspecs):
        old = ImageName.parse(obj[key])
        new = replacement_pullspecs.get(old)
        if new is not None and new != old:
            log.debug("%s - Replaced pullspec: %s -> %s", self.path, old, new)
            obj[key] = new.to_str()  # `new` is an ImageName

    def _replace_pullspecs_everywhere(self, obj, k_or_i, replacement_pullspecs):
        item = obj[k_or_i]
        if isinstance(item, CommentedMap):
            for k in item:
                self._replace_pullspecs_everywhere(item, k, replacement_pullspecs)
        elif isinstance(item, CommentedSeq):
            for i in range(len(item)):
                self._replace_pullspecs_everywhere(item, i, replacement_pullspecs)
        elif isinstance(item, str):
            # Doesn't matter if string was not a pullspec, it will simply not match anything
            # in replacement_pullspecs and no replacement will be done
            self._replace_unnamed_pullspec(obj, k_or_i, replacement_pullspecs)


class OperatorManifest(object):
    """
    A collection of operator files.

    Currently, only ClusterServiceVersion files are considered relevant.
    """

    def __init__(self, files):
        """
        Initialize an OperatorManifest

        :param files: list of OperatorCSVs
        """
        self.files = files

    @classmethod
    def from_directory(cls, path):
        """
        Make an OperatorManifest from all the relevant files found in
        a directory (or its subdirectories)

        :param path: Path to directory
        :return: OperatorManifest
        """
        if not os.path.isdir(path):
            raise RuntimeError("Path does not exist or is not a directory: {}".format(path))
        yaml_files = cls._get_yaml_files(path)
        operator_csvs = list(cls._get_csvs(yaml_files))
        return cls(operator_csvs)

    @classmethod
    def _get_yaml_files(cls, dirpath):
        for d, _, files in os.walk(dirpath):
            for f in files:
                if f.endswith(".yaml") or f.endswith(".yml"):
                    yield os.path.join(d, f)

    @classmethod
    def _get_csvs(cls, yaml_files):
        for f in yaml_files:
            try:
                yield OperatorCSV.from_file(f)
            except NotOperatorCSV:
                pass
