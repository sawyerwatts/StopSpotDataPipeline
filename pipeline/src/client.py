import os
import sys
from datetime import datetime
from datetime import timedelta
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from src.ios import IOs
from src.tables import CTran_Data
from src.tables import Flagged_Data
from src.tables import Flags
from src.tables import Service_Periods
from src.config import config
from src.restarter import restarter
from src.interface import ArgInterface
from flaggers.flagger import flaggers
from flaggers.flagger import Flags as flag_enums


class _Option():
    def __init__(self, msg, func_pointer):
        self.msg = msg
        self.func_pointer = func_pointer

""" Members:
self.config
self. tables
self._hive_engine
self._portal_engine
self._output_path
self._output_type
"""
class _Client(IOs):
    def __init__(self, read_env_data=True, verbose=True):
        super().__init__(verbose)
        try:
            if not read_env_data and os.environ["PIPELINE_ENV_DATA"]:
                read_env_data = True
        except KeyError as err:
            pass

        self.config = config
        self.config.load(read_env_data=read_env_data)

        self._output_path = config.get_value("output_path")
        self._output_type = config.get_value("output_type")

        portal_user = config.get_value("portal_user")
        portal_passwd = config.get_value("portal_passwd")
        portal_hostname = config.get_value("portal_hostname")
        portal_db_name = config.get_value("portal_db_name")
        portal_schema = config.get_value("portal_schema")
        if portal_user and portal_passwd and portal_hostname and portal_db_name :
            if portal_schema:
                self.ctran = CTran_Data(portal_user, portal_passwd, portal_hostname, portal_db_name, portal_schema, verbose=verbose)
            else:
                self.ctran = CTran_Data(portal_user, portal_passwd, portal_hostname, portal_db_name, verbose=verbose)
        else:
            print("Please enter credentials for Portals database with the C-Tran table.")
            self.ctran = CTran_Data(verbose=verbose)
        self._portal_engine = self.ctran.get_engine()
        pipe_user = config.get_value("pipeline_user")
        pipe_passwd = config.get_value("pipeline_passwd")
        pipe_hostname = config.get_value("pipeline_hostname")
        pipe_db_name = config.get_value("pipeline_db_name")
        pipe_schema = config.get_value("pipeline_schema")
        if pipe_user and pipe_passwd and pipe_hostname and pipe_db_name:
            if pipe_schema:
                self.flagged = Flagged_Data(pipe_user, pipe_passwd, pipe_hostname, pipe_db_name, pipe_schema, verbose=verbose)
                self._hive_engine = self.flagged.get_engine()
                engine_url = self._hive_engine.url
                self.flags = Flags(schema=pipe_schema, verbose=verbose, engine=engine_url)
                self.service_periods = Service_Periods(schema=pipe_schema, verbose=verbose, engine=engine_url)
                return
            else:
                self.flagged = Flagged_Data(pipe_user, pipe_passwd, pipe_hostname, pipe_db_name, verbose=verbose)
        else:
            print("Please enter credentials for Hive's Database.")
            self.flagged = Flagged_Data(verbose=verbose)

        self._hive_engine = self.flagged.get_engine()
        engine_url = self._hive_engine.url
        self.flags = Flags(verbose=verbose, engine=engine_url)
        self.service_periods = Service_Periods(verbose=verbose, engine=engine_url)

    #######################################################

    def main(self, read_env_data=False):

        if len(sys.argv) > 1:
            ai = ArgInterface()
            return ai.query_with_args(self, self.ctran, self.flagged, sys.argv[1:])

        options = [
            _Option("(or ctrl-d) Exit.", lambda: "Exit"),
            _Option("Choose output type (defaults to aperture).", 
                        self._output_menu),
            _Option("[dev tool] Create Aperature, the Portal mock DB [dev tool].",
                        self.ctran.create_table),
            _Option("Create Hive, the output point of the Data Pipeline.",
                        self.create_hive),
            _Option("Process data from Portal (Which currently is Aperture)",
                        self.process_data),
            _Option("Process the next unproccessed service date from Portal (Which currently is Aperture)",
                        self.process_next_day),
            _Option("Process the all following unproccessed service dates from Portal (Which currently is Aperture)",
                        self.process_since_checkpoint),
            _Option("Reprocess service date(s)",
                        self.reprocess),
            _Option("Create all views",
                        self.create_all_views),
            _Option("Sub-menu: DB Operations",
                        self._db_menu),
        ]

        self._menu("Welcome to the CTran Data Marking Pipeline.", options)

    #######################################################

    def create_hive(self):
        if (self._output_type == "aperture" or self._output_type == "both"):
            self.flags.create_table()
            self.service_periods.create_table()
            self.flagged.create_table()

        #When dealing with csv and hive creation, only data available right away is flags, so we only save flags to csv when "creating" csv-hive
        if (self._output_type == "csv" or self._output_type == "both"):
            if not self.flags.write_csv(self._output_path):
                print("Error saving Flags to csv.")

            self.service_periods.create_table()

    ###########################################################

    # Process data between start_date and end_date, inclusive. These parameters
    # can be Date instances or strings in format "YYYY/MM/DD". If no dates are
    # supplied, this will prompt the user for them.
    def process_data(self, start_date=None, end_date=None, restart=False):
        self.print("Starting data processing pipeline.")
        start_date, end_date = self._get_date_range(start_date, end_date)
        ctran_df = self.ctran.query_date_range(start_date, end_date)
        if ctran_df is None or ctran_df.empty:
            self.print("ERROR: the supplied dates were unable to be gathered from CTran data.")
            return False

        flagged_rows = []
        skipped_rows = 0
        csv_service_keys = []
        # TODO: Stackoverflow is telling me iterrows is a slow way of iterrating,
        # but i'll leave optimizing for later.
        self.print("Processing the queried data.")
        duplicate = None
        for row_id, row in ctran_df.iterrows():

            if self._output_type == "csv" or self._output_type == "both":
                if not row.service_date in csv_service_keys:
                    csv_service_keys.append(row.service_date)

            service_key = self.service_periods.query_or_insert(row.service_date)

            if restart:
                if config.get_value('max_skipped_rows'):
                    if skipped_rows > config.get_value('max_skipped_rows'):
                        msg = "ERROR: exceeded maximum number of skipped service rows."
                        self.print(msg)
                        restarter.critical_error(msg)

            # If this fails, it's very likely a sqlalchemy error.
            # e.g. not able to connect to db.
            if not service_key:
                self.print("ERROR: cannot find or create new service_key, skipping.")
                skipped_rows +=1
                continue

            flags = set()
            for flagger in flaggers:
                try:
                    # Duplicate flagger requires a special call later on,
                    # independent of this loop.
                    if flagger.name == "Duplicate":
                        duplicate = flagger
                    else:
                        flags.update(flagger.flag(row, config))
                except Exception as e:
                    self.print("WARNING: error in flagger {}. Skipping.\n{}"
                        .format(flagger.name, e))

            date = row["service_date"]
            date = "".join([str(date.year), "/", str(date.month), "/", str(date.day)])
            for flag in flags:
                flagged_rows.append([
                    row_id,
                    service_key,
                    int(flag),
                    date
                ])

        # Duplicate flagger requires a special call later on, independent of
        # the primary loop.
        if duplicate is not None:
            flagged_rows.extend(self._flag_duplicates(ctran_df, duplicate))
        else:
            self.print("NOTE: this run is not checking for duplicates.")

        #Saving result
        if self._output_type == "aperture":
            self.flagged.write_table(flagged_rows)
        if self._output_type == "csv":
            self.flagged.write_csv(self._output_path, flagged_rows)
            self.service_periods.write_csv(self._output_path, csv_service_keys)
        if self._output_path == "both":
            self.flagged.write_table(flagged_rows)
            self.flagged.write_csv(self._output_path, flagged_rows)
            self.service_periods.write_csv(self._output_path, csv_service_keys)

        self.print("Done executing the pipeline.")
        return True

    ###########################################################

    # This method will process all days since the latest processed day.
    def process_since_checkpoint(self):
        start_date = self.flagged.get_latest_day()
        if start_date is None:
            self.print("ERROR: no prior date processed; cannot continue from the last processed day.")
            return False
        self.print("Last processed day: " + str(start_date))
        start_date = start_date + timedelta(days=1)
        self.print("Processing from:  " + str(start_date))
        end_date = datetime.now().date()
        self.print("\t   until: " + str(end_date))
        return self.process_data(start_date, end_date)

    ###########################################################
    
    # This method will process the next day after the latest processed day.
    def process_next_day(self, restart=False):
        start_date = self.flagged.get_latest_day()
        if start_date is None:
            msg = "ERROR: an error occured while attempting to find the last processed day. This may be because the last processed day doesn't exist or an error occured while connecting to the database."
            self.print(msg)
            if restart:
                restarter.critical_error(msg)
            else:
                return False
            

        self.print("Last processed day: " + str(start_date))
        start_date = start_date + timedelta(days=1)
        self.print("Processing from:  " + str(start_date))
        end_date = start_date
        self.print("\t   until: " + str(end_date))
        return self.process_data(start_date, end_date, restart)

    ###########################################################

    def reprocess(self, start_date=None, end_date=None):
        start_date, end_date = self._get_date_range(start_date, end_date)
        if not self.flagged.delete_date_range(start_date, end_date):
            self.print()
            return False
        return self.process_data(start_date, end_date)

    ###########################################################

    def create_all_views(self):
        return self.flagged.create_views_all_flags()

    #######################################################

    def _flag_duplicates(self, df, duplicate_instance):
        """ Order of fields.
            index:  row_id
            field0: service_key
            field1: date
            field2: flag
        """
        dup_df = None
        try:
            dup_df = duplicate_instance.flag(df, config)
        except ValueError as err:
            self.print("ERROR:", err)
            return []

        dup_df.insert(0, "service_key", 0)
        dup_df["service_key"] = dup_df["service_date"].apply(
            lambda date: self.service_periods.query_or_insert(date))

        dup_df.insert(1, "flag_id", 1)
        dup_df["flag_id"] = flag_enums.DUPLICATE

        dup_df["service_date"] = dup_df["service_date"].apply(
            lambda date: "".join([str(date.year), "/", str(date.month), "/", str(date.day)]))

        indices = dup_df.index.tolist()
        values = dup_df.values.tolist()
        dup_list = []
        for i in range(len(indices)):
            temp = [indices[i]]
            temp.extend(values[i])
            dup_list.append(temp)

        return dup_list

    ###########################################################

    def _db_menu(self):
        def ctran_info():
            query = self.ctran.get_full_table()
            if query is None:
                self.print("WARNING: no data returned.")
            else:
                query.info()

        options = [
            _Option("(or ctrl-d) Exit.", lambda: "Exit"),
            _Option("Print engine.", lambda: print(self.flagged.get_engine())),
            _Option("[dev tool] Create aperture schema [dev tool].", self.ctran.create_schema),
            _Option("[dev tool] Delete aperture schema [dev tool].", self.ctran.delete_schema),
            _Option("[dev tool] Create mock ctran_data table [dev tool].", self.ctran.create_table),
            _Option("[dev tool] Delete mock ctran_data table [dev tool].", self.ctran.delete_table),
            _Option("Create hive schema.", self.flags.create_schema),
            _Option("Delete hive schema.", self.flags.delete_schema),
            _Option("Create flagged_data table.", self.flagged.create_table),
            _Option("Create flags table.", self.flags.create_table),
            _Option("Create service_periods table.", self.service_periods.create_table),
            _Option("Delete flagged_data table.", self.flagged.delete_table),
            _Option("Delete service_periods table.", self.flags.delete_table),
            _Option("Query ctran_data and print ctran_data.info().", ctran_info)
        ]

        return self._menu("This is the Database Operations sub-menu.", options)

    ###########################################################

    # Option.func_pointer should return str "Exit" iff that option should cause
    # the menu it is in to exit.
    def _menu(self, title, options):
        if len(options) == 0:
            return

        msg = " ".join([str(title), "Please select what you would like to do:"])
        should_exit = False
        while not should_exit:
            print()
            print(msg)
            print()
            print()
            for i in range(len(options)):
                print(str(i) + ": " + options[i].msg)
            print()

            option = None
            try:
                option = self._get_menu_option(0, len(options)-1)
            except EOFError:
                option = 0

            if options[option].func_pointer() == "Exit":
                should_exit = True

    ###########################################################

    def _get_menu_option(self, min_value, max_value, cli_symbol="> "):
        should_continue = True
        while should_continue:
            try:
                option = input(cli_symbol)
                option = int(option)

                if option < min_value or option > max_value:
                    print("{" + str(option) + "} is not within range [" + str(min_value) + ", " + str(max_value) + "]; try again.")
                else:
                    should_continue = False

            except ValueError:
                print("Please enter an integer.")

        return option

    ###########################################################

    # start_date and end_date can be string dates in YYYY/MM/DD, datetimes, or
    # None. If start_date is none, the user will be prompted for the start and
    # end dates. If end_date is none, the start_date will be used for that
    # value. If dates are backwards, they will be flipped.
    # This returns: start_date, end_date
    def _get_date_range(self, start_date=None, end_date=None):
        def _convert_str_to_date(string, criteria):
            try:
                if isinstance(string, str):
                    string = datetime.strptime(string, "%Y/%m/%d")
            except ValueError:
                string = _get_valid_date(criteria)
            return string

        def _get_valid_date(criteria):
            while True:
                date = self.prompt("Please enter the " + criteria + " date (YYYY/MM/DD): ")
                try:
                    date = datetime.strptime(date, "%Y/%m/%d")
                except ValueError:
                    print("\tThe input date is malformed; please use the YYYY/MM/DD format.")
                else:
                    return date

        if start_date is None:
            start_date = _get_valid_date("start")
            end_date   = _get_valid_date("end  ")

        else:
            start_date = _convert_str_to_date(start_date, "start")

            if end_date is None:
                end_date = start_date
            else:
                end_date = _convert_str_to_date(end_date, "end  ")

        if start_date < end_date:
            return start_date, end_date
        else:
            return end_date, start_date

    ###########################################################

    def _output_menu(self):

        def change_to_aperture(): 
            self._output_type = "aperture"
            print("Output will be saved to aperture")
        
        def change_to_csv(): 
            self._output_type = "csv"
            print("Output will be saved to csv's")
        
        def change_to_both(): 
            self._output_type = "both"
            print("Output will be saved to aperture AND csv's")

        options = [
           _Option("(or ctrl-d) Exit.", lambda: "Exit"),
           _Option("Check current output type.", lambda: print("Current output type: " + self._output_type)),
           _Option("Check current output path.", lambda: print("Current output path: " + self._output_path)),
           _Option("Change output to Aperture.", change_to_aperture),
           _Option("Change output to CSV's.", change_to_csv),
           _Option("Change output to Aperture AND CSV's.", change_to_both)
        ]

        return self._menu("This is output type sub-menu.", options)

