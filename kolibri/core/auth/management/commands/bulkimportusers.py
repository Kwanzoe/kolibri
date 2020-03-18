import csv
import logging
import re
import sys

from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.core.management.base import CommandError

from kolibri.core.auth.constants.demographics import choices
from kolibri.core.auth.csv_utils import input_fields
from kolibri.core.auth.csv_utils import labels
from kolibri.core.auth.models import Classroom
from kolibri.core.auth.models import Facility
from kolibri.core.auth.models import FacilityUser
from kolibri.core.tasks.management.commands.base import AsyncCommand

logger = logging.getLogger(__name__)
DEFAULT_PASSWORD = make_password("kolibri")
fieldnames = ("Username", "Password", "Full name", "User type", "Identifier", "Birth year", "Gender", "Enrolled in", "Assigned to")

# Validators ###


def number_range(min, max):
    """
    Return a value check function which raises a ValueError if the supplied
    value is less than `min` or greater than `max`.
    """
    def checker(v):
        if int(v) < min or int(v) > max:
            raise ValueError(v)

    return checker


def value_length(length, null=False):
    """
    Return a value check function which raises a ValueError if the supplied
    value has a length greater than 'length'
    If null is not True raises a ValueError if the supplied value is None.
    """
    def checker(v):
        if null and v is None:
            return checker
        if len(v) > length:
            raise ValueError(v)

    return checker


def enumeration(*args):
    """
    Return a value check function which raises a ValueError if the value (case
    insensitive) is not in the enumeration of values provided by args.
    """

    assert len(args) > 0, 'at least one argument is required'
    if len(args) == 1:
        # assume the first argument defines the membership
        members = args[0].lower()
    else:
        members = tuple(map(str.lower, args))

    def checker(value):
        if value.lower() not in members:
            raise ValueError(value)
    return checker


def valid_name(username=True, null=False):
    """
    Return a value check function which raises a ValueError if the value has
    some of the punctuaction chars that are not allowed.
    If username is False it allows spaces, slashes and hyphens.
    If null is not True raises a ValueError if the supplied value is None.
    """
    def checker(v):
        if null and v is None:
            return checker
        has_punc = "/[\s`~!@#$%^&*()\-+={}\[\]\|\\\/:;\"'<>,\.\?]/"  # noqa
        if not username:
            has_punc = "/[`~!@#$%^&*()\+={}\[\]\|\\\/:;\"'<>\.\?]/"  # noqa
        if re.match(has_punc, v):
            raise ValueError(v)
    return checker


class Validator(object):
    """
    Class to apply different validation checks on a CSV data reader.
    """
    def __init__(self, header_names):
        self._header_names = header_names
        self._checks = list()
        self.classrooms = dict()
        self.coach_classrooms = dict()
        self.users = dict()

    def add_check(self, header_name, check, message):
        """
        Add a header check, i.e., check whether the header record is consistent
        with the expected field names.
        `header_name` - name of the header for the column to be checked
        `check`- function to be used as validator of the values in the column
        `message` - problem message to report if a value is not valid
        """
        self._checks.append((header_name, check, message))

    def get_username(self, row):
        username = row.get('Username')
        if username in self.users.keys():
            return None

        return username

    def check_classroom(self, row, username):
        def append_users(group, key):
            try:
                groups = row.get(key, None).split(",")
                for classroom in groups:
                    if not classroom:
                        continue
                    if classroom in group:
                        group[classroom].append(username)
                    else:
                        group[classroom] = [username, ]
            except AttributeError:
                # there are not members of 'key'
                pass

        # enrolled learners:
        append_users(self.classrooms, "Enrolled in")

        # assigned coaches
        if row.get('User type', 'learner').lower != "learner":
            # a student can't be assigned to coach a classroom
            append_users(self.coach_classrooms, "Assigned to")

    def validate(self, data):
        """
        Validate `data` and return an iterator over errors found.
        """
        for index, row in enumerate(data):
            error_flag = False
            username = self.get_username(row)
            if not username:
                error = {
                    "row": index + 1,
                    "message": "Duplicated username",
                    "field": "Username",
                    "value": row.get('Username'),
                }
                error_flag = True
                yield error

            for header_name, check, message in self._checks:
                value = row[header_name]
                try:
                    check(value)
                except ValueError:
                    error = {
                        "row": index + 1,
                        "message": message,
                        "field": header_name,
                        "value": value,
                    }
                    error_flag = True
                    yield error
                except Exception as e:
                    error = {
                        "row": index + 1,
                        "message": "Unexpected error [%s]: %s"
                        % (e.__class__.__name__, e),
                        "field": header_name,
                        "value": value,
                    }
                    error_flag = True
                    yield error
            # if there aren't any errors, let's add the user and classes
            if not error_flag:
                self.check_classroom(row, username)
                self.users[username] = row


class Command(AsyncCommand):
    def add_arguments(self, parser):
        parser.add_argument(
            "filepath", action="store", type=str, help="Path to CSV file."
        )
        parser.add_argument(
            "--facility",
            action="store",
            type=str,
            help="Facility id to import the users into",
        )
        parser.add_argument(
            "--dryrun",
            action="store",
            type=bool,
            default=True,
            help="Validate data without doing actual database updates",
        )
        parser.add_argument(
            "--delete",
            action="store",
            type=bool,
            default=True,
            help="Delete all users in the facility not included in this import (excepting actual user)",
        )

        parser.add_argument(
            "--userid",
            action="store",
            type=str,
            default=None,
            help="Id of the user executing the command, it will not be deleted in case deleted is set",
        )

    def csv_values_validation(self, reader):
        csv_errors = []
        validator = Validator(self.fieldnames)
        validator.add_check("Full name", value_length(125), "Full Name is too long")
        validator.add_check("Birth year", number_range(1900, 99999), "Not a valid year")
        validator.add_check("Username", value_length(125), "User name is too long")
        validator.add_check("Username", valid_name(), "Username only can contain characters, numbers and underscores")
        validator.add_check("Password", value_length(128), "Password is too long")
        validator.add_check("User type", enumeration("Learner", "Admin", "Facility coach", "Class coach"), "Not a valid user type")
        # validator.add_check("Gender", enumeration(tuple(val[1] for val in choices)), "Not a valid gender")
        validator.add_check("Gender", enumeration(*tuple(val[0] for val in choices)), "Not a valid gender")
        validator.add_check("Identifier", value_length(64), "Identifier is too long")
        validator.add_check("Enrolled in", value_length(100, null=True), "Class name is too long")
        validator.add_check("Assigned to", value_length(100, null=True), "Class name is too long")
        # validator.add_check("Enrolled in", valid_name(username=False, null=True), "A class name only can contain characters, numbers and underscores")
        # validator.add_check("Assigned to", valid_name(username=False, null=True), "A class name only can contain characters, numbers and underscores")

        row_errors = validator.validate(reader)
        for err in row_errors:
            csv_errors.append(err)
        return (csv_errors, (validator.classrooms, validator.coach_classrooms), validator.users)

    def csv_headers_validation(self, options):
        # open using default OS encoding
        with open(options["filepath"]) as f:
            header = next(csv.reader(f, strict=True))
            has_header = False
            if all(col in self.fieldnames for col in header):
                # Every item in the first row matches an item in the fieldnames, it is a header row
                if (
                    "username" not in header
                    and str(labels["username"]) not in header
                ):
                    self.errors.append(
                        "No usernames specified, this is required for user creation"
                    )
                    raise CommandError(self.errors[-1])
                has_header = True
            elif any(col in self.fieldnames for col in header):
                self.errors.append(
                    "Mix of valid and invalid header labels found in first row"
                )
                raise CommandError(self.errors[-1])
        return has_header

    def build_users_objects(self, users):
        new_users = list()
        update_users = list()
        existing_users = FacilityUser.objects.filter(facility=self.default_facility).filter(username__in=users.keys()).values_list('username', flat=True)

        # creating the users takes half of the time
        progress = (100 / self.number_lines) * .5

        for user in users:
            self.progress_update(progress)
            if user in existing_users:
                user_obj = FacilityUser.objects.get(username=user, facility=self.default_facility)
            else:
                user_obj = FacilityUser(username=user, facility=self.default_facility)
                user_obj.id = user_obj.calculate_uuid()
            user_row = users[user]
            password = user_row.get("Password", None)
            if password:
                user_obj.set_password(password)
            else:
                user_obj.password = DEFAULT_PASSWORD

            # demographics:
            value = user_row.get('Gender', None)
            if value:
                user_obj.gender = value.strip().upper()

            value = user_row.get("Birth year", None)
            if value:
                user_obj.birth_year = value

            value = user_row.get("Identifier", None)
            if value:
                user_obj.id_number = value

            if user in existing_users:
                update_users.append(user_obj)
            else:
                new_users.append(user_obj)

        return (new_users, update_users)

    def db_validate_list(self, db_list, users=False):
        errors = []
        # validating the users takes aprox 40% of the time
        if users:
            progress = (100 / self.number_lines) * .4 * (len(db_list) / self.number_lines)
        for obj in db_list:
            if users:
                self.progress_update(progress)
            try:
                obj.full_clean()
            except ValidationError as e:
                for message in e.message_dict:
                    error = {
                        "row": str(obj),
                        "message": e.message_dict[message][0],
                        "field": message,
                        "value": vars(obj)[message],
                    }
                    errors.append(error)

        return errors

    def build_classes_objects(self, classes):
        new_classes = list()
        update_classes = list()
        total_classes = set([k for k in classes[0].keys()] + [v for v in classes[1].keys()])
        existing_classes = Classroom.objects.filter(parent=self.default_facility).filter(name__in=total_classes).values_list('name', flat=True)

        for classroom in total_classes:
            if classroom in existing_classes:
                class_obj = Classroom.objects.get(name=classroom, parent=self.default_facility)
                update_classes.append(class_obj)
            else:
                class_obj = Classroom(name=classroom, parent=self.default_facility)
                class_obj.id = class_obj.calculate_uuid()
                new_classes.append(class_obj)
        self.progress_update(1)
        return (new_classes, update_classes)

    def get_facility(self, options):
        if options["facility"]:
            default_facility = Facility.objects.get(pk=options["facility"])
        else:
            default_facility = Facility.get_default_facility()
        if not default_facility:
            self.errors.append(
                "No default facility exists, please make sure to provision this device before running this command"
            )
            raise CommandError(self.errors[-1])

        return default_facility

    def get_number_lines(self, options):
        try:
            with open(options["filepath"]) as f:
                number_lines = len(f.readlines())
        except (ValueError, IOError, csv.Error) as e:
            self.errors.append("Error trying to write csv file: {}".format(e))
            logger.error(self.errors[-1])
            sys.exit(1)
        return number_lines

    def get_delete(self, options, update_users, update_classes):
        if not options["delete"]:
            return ([], [])
        users_not_to_delete = [u.id for u in update_users]
        admins = self.default_facility.get_admins()
        users_not_to_delete += admins.values_list('id', flat=True)
        if options["userid"]:
            users_not_to_delete.append(options["userid"])
        users_to_delete = FacilityUser.objects.filter(facility=self.default_facility).exclude(id__in=users_not_to_delete).values_list('id', flat=True)

        classes_not_to_clear = [c.id for c in update_classes]
        classes_to_clear = Classroom.objects.filter(parent=self.default_facility).exclude(id__in=classes_not_to_clear).values_list('id', flat=True)

        return (users_to_delete, classes_to_clear)

    def handle_async(self, *args, **options):
        self.errors = []
        self.default_facility = self.get_facility(options)
        self.fieldnames = fieldnames
        self.number_lines = self.get_number_lines(options)

        with self.start_progress(total=100) as self.progress_update:
            # validate csv headers:
            has_header = self.csv_headers_validation(options)
            self.progress_update(1)  # state=csv_headers

            try:
                with open(options["filepath"]) as f:
                    if has_header:
                        reader = csv.DictReader(f, strict=True)
                    else:
                        reader = csv.DictReader(f, fieldnames=input_fields, strict=True)
                    csv_errors, classes, users = self.csv_values_validation(reader)
            except (ValueError, IOError, csv.Error) as e:
                self.errors.append("Error trying to write csv file: {}".format(e))
                logger.error(self.errors[-1])
                sys.exit(1)

            db_new_users, db_update_users = self.build_users_objects(users)
            db_new_classes, db_update_classes = self.build_classes_objects(classes)

            users_to_delete, classes_to_clear = self.get_delete(options, db_update_users, db_update_classes)
            csv_errors += self.db_validate_list(db_new_users, users=True)
            csv_errors += self.db_validate_list(db_update_users, users=True)
            # progress = 91%
            csv_errors += self.db_validate_list(db_new_classes)
            csv_errors += self.db_validate_list(db_update_classes)

            if options["dryrun"]:
                print(csv_errors)
                print(len(db_new_classes))
                print(len(db_update_classes))
                print(len(db_new_users))
                print(len(db_update_users))
                print(len(users_to_delete))
                print(len(classes_to_clear))
            else:
                print("Saving...")
