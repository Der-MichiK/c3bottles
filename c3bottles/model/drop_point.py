import json
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Union

from flask_babel import LazyString, lazy_gettext
from sqlalchemy import desc

from c3bottles import app, db
from c3bottles.lib import metrics
from c3bottles.model.category import Category, all_categories
from c3bottles.model.location import Location
from c3bottles.model.report import Report
from c3bottles.model.visit import Visit


class DropPoint(db.Model):
    """
    A location in the venue for visitors to drop their empty bottles.

    A drop point consists of a sign "bottle drop point <number>" at the
    wall which tells visitors that a drop point should be present there
    and a number of empty crates to drop bottles into.

    If the `removed` column is not null, the drop point has been removed
    from the venue (numbers are not reassigned).

    Each drop point is referenced by a unique number, which is
    consequently the primary key to identify a specific drop point. Since
    the location of drop points may change over time, it is not simply
    saved in the table of drop points but rather a class itself.
    """

    number = db.Column(db.Integer, primary_key=True, autoincrement=False)
    category_id = db.Column(db.Integer, nullable=False, default=1)
    time = db.Column(db.DateTime)
    removed = db.Column(db.DateTime)
    locations = db.relationship("Location", order_by="Location.time")
    reports = db.relationship("Report", lazy="dynamic")
    visits = db.relationship("Visit", lazy="dynamic")

    _last_state = db.Column(
        db.Enum(*Report.states, name="drop_point_states"),
        default=Report.states[1],
        name="last_state",
    )

    def __init__(
        self,
        number: int,
        category_id: int = 0,
        description: str = None,
        lat: float = None,
        lng: float = None,
        level: int = None,
        time: datetime = None,
    ):
        """
        Create a new drop point object.

        New drop point objects will be added to the database automatically
        if the creation was successful. A location will also be added
        automatically.

        :raises ValueError: If an error occurred during creation of the drop
            point. The error message will contain a tuple of dicts which
            indicate in which part of the creation the error occurred.
        """

        errors: List[Dict[str, LazyString]] = []

        try:
            self.number = int(number)
        except (TypeError, ValueError):
            errors.append({"number": lazy_gettext("Drop point number is not a number.")})
        else:
            if self.number < 1:
                errors.append({"number": lazy_gettext("Drop point number is not positive.")})
            if DropPoint.query.get(self.number):
                errors.append({"number": lazy_gettext("That drop point already exists.")})

        if category_id in all_categories:
            self.category_id = category_id
        else:
            errors.append({"cat_id": lazy_gettext("Invalid drop point category.")})

        if time is not None and not isinstance(time, datetime):
            errors.append({"DropPoint": lazy_gettext("Creation time not a datetime object.")})

        if isinstance(time, datetime) and time > datetime.now():
            errors.append({"DropPoint": lazy_gettext("Creation time in the future.")})

        self.time = time if time else datetime.now()

        if errors:
            raise ValueError(*errors)

        try:
            Location(
                self,
                time=self.time,
                description=description,
                lat=lat,
                lng=lng,
                level=level,
            )
        except ValueError as e:
            errors += e.args
            raise ValueError(*errors)

        db.session.add(self)
        db.session.commit()

        metrics.drop_point_count.labels(
            state=self.last_state, category=self.category.metrics_name
        ).inc()

    def remove(self, time: datetime = None) -> None:
        """
        Remove a drop point.

        This will not actually purge a drop point from the database but
        simply mark it as removed so it will no longer show up in the
        frontend. The time of removal can be given optionally and will
        default to :func:`datetime.today()`.
        """

        if self.removed:
            raise RuntimeError({"DropPoint": lazy_gettext("Drop point already removed.")})

        if time and not isinstance(time, datetime):
            raise TypeError({"DropPoint": lazy_gettext("Removal time not a datetime object.")})

        if time and time > datetime.now():
            raise ValueError({"DropPoint": lazy_gettext("Removal time in the future.")})

        self.removed = time if time else datetime.now()
        metrics.drop_point_count.labels(
            state=self.last_state, category=self.category.metrics_name
        ).dec()

    def report(self, state=None, time=None) -> None:
        """
        Submit a report for a drop point.
        """
        Report(self, time=time, state=state)

    def visit(self, action=None, time=None) -> None:
        """
        Perform a visit of a drop point.
        """
        Visit(self, time=time, action=action)

    @property
    def category(self) -> Category:
        return Category.get(self.category_id)

    @property
    def level(self) -> Optional[int]:
        return self.locations[-1].level if self.locations else None

    @property
    def lat(self) -> Optional[float]:
        return self.locations[-1].lat if self.locations else None

    @property
    def lng(self) -> Optional[float]:
        return self.locations[-1].lng if self.locations else None

    @property
    def description(self) -> Optional[Union[str, LazyString]]:
        return self.locations[-1].description if self.locations else None

    @property
    def description_with_level(self) -> Union[str, LazyString]:
        map_source = app.config.get("MAP_SOURCE", {})
        if len(map_source.get("level_config", [])) > 1:
            return lazy_gettext(
                "%(location)s on level %(level)i",
                location=self.description if self.description else lazy_gettext("somewhere"),
                level=self.level,
            )
        else:
            return self.description if self.description else lazy_gettext("somewhere")

    @property
    def location(self) -> Optional[Location]:
        return self.locations[-1] if self.locations else None

    @property
    def total_report_count(self) -> int:
        return self.reports.count()

    @property
    def new_report_count(self) -> int:
        last_visit = self.last_visit
        if last_visit:
            return self.reports.filter(Report.time > last_visit.time).count()
        else:
            return self.total_report_count

    @property
    def last_state(self) -> str:
        """
        Get the current state of a drop point.

        The state is influenced by two mechanisms:

        * Reports: a report will always set the drop point to the state
          that has been reported by the reporter, irrespective of the
          state before.
        * Visits: If a visit was performed since the last report, the
          drop point is now either empty or unchanged and therefore,
          if the drop point was emptied, the empty state is returned.
          If the drop point was not emptied during the visit, the last
          reported state will be returned.

        If neither reports nor visits have been recorded yet or only visits
        without any actions, the drop point state is returned as new.
        """
        return self._last_state

    @last_state.setter
    def last_state(self, state: str):
        metrics.drop_point_count.labels(
            state=self.last_state, category=self.category.metrics_name
        ).dec()
        metrics.drop_point_count.labels(state=state, category=self.category.metrics_name).inc()
        self._last_state = state

    @property
    def last_report(self) -> Optional[Report]:
        """
        Get the last report of a drop point.
        """
        return self.reports.order_by(Report.time.desc()).first()

    @property
    def last_visit(self) -> Optional[Visit]:
        """
        Get the last visit of a drop point.
        """
        return self.visits.order_by(Visit.time.desc()).first()

    @property
    def new_reports(self) -> Iterable[Report]:
        """
        Get the reports since the last visit of a drop point.

        This method returns all reports for this drop point since the last
        visit ordered descending by time, i.e. the newest report is the first
        in the list. If no visits have been recorded yet, all reports are
        returned.
        """
        last_visit = self.last_visit
        if last_visit:
            return (
                self.reports.filter(Report.time > last_visit.time)
                .order_by(Report.time.desc())
                .all()
            )
        else:
            return self.reports.order_by(Report.time.desc()).all()

    @property
    def history(self) -> Iterable[Dict[str, Any]]:
        history = []

        for visit in self.visits.all():
            history.append({"time": visit.time, "visit": visit})

        for report in self.reports.all():
            history.append({"time": report.time, "report": report})

        for location in self.locations:
            history.append({"time": location.time, "location": location})

        history.append({"time": self.time, "drop_point": self})

        if self.removed:
            history.append({"time": self.removed, "removed": True})

        return sorted(history, key=lambda k: k["time"], reverse=True)

    @property
    def visit_interval(self) -> int:
        """
        Get the visit interval for this drop point.

        This method returns the visit interval for this drop point
        in seconds.

        This is not implemented as a static method or a constant
        since in the future the visit interval might depend on
        the location of drop points, time of day or a combination
        of those.
        """
        return 60 * app.config.get("BASE_VISIT_INTERVAL", 120)

    @property
    def priority_factor(self) -> float:
        return 0
        """
        Get the priority factor.

        This factor determines how often a drop point should be visited.
        The factor depends on the severity of the reports submitted.
        """

        # The priority of a removed drop point obviously is always 0.
        if self.removed:
            return 0

        new_reports = self.new_reports

        # This is the starting priority. The report weight should
        # be scaled relative to 1, so this can be interpreted as a
        # number of standing default reports ensuring that every
        # drop point's priority increases slowly if it is not
        # visited even if no real reports come in.
        priority = app.config.get("DEFAULT_VISIT_PRIORITY", 1)

        i = 0
        for report in new_reports:
            priority += report.get_weight() / 2**i
            i += 1

        priority /= 1.0 * self.visit_interval

        return priority

    @property
    def priority_base_time(self) -> datetime:
        """
        Get the base time for visit priority calculation of a drop point.

        This is either the time of the last visit, or, if no visit has been
        performed yet, the creation time of the drop point.
        """
        if self.last_visit:
            return self.last_visit.time
        else:
            return self.time

    @property
    def priority(self) -> float:
        """
        Get the priority to visit this drop point.

        The priority to visit a drop point mainly depends on the
        number and weight of reports since the last visit.

        In addition, priority increases with time since the last
        visit even if the states of reports indicate a low priority.
        This ensures that every drop point is visited from time to
        time.
        """
        priority = (
            self.priority_factor * (datetime.today() - self.priority_base_time).total_seconds()
        )

        return round(priority, 2)

    @classmethod
    def get_dp_info(cls, number: int) -> Optional[Dict[str, Any]]:
        dp = cls.query.get(number)
        if dp is not None:
            return {
                "number": dp.number,
                "category_id": dp.category_id,
                "category": str(dp.category),
                "description": dp.description,
                "description_with_level": str(dp.description_with_level),
                "reports_total": dp.total_report_count,
                "reports_new": dp.new_report_count,
                "priority": dp.priority,
                "priority_factor": dp.priority_factor,
                "base_time": dp.priority_base_time.strftime("%s"),
                "last_state": dp.last_state,
                "removed": True if dp.removed else False,
                "lat": dp.lat,
                "lng": dp.lng,
                "level": dp.level,
            }
        else:
            return None

    @classmethod
    def get_dp_json(cls, number: int, indent: Optional[int] = 4 if app.debug else None) -> str:
        """
        Get a JSON string characterizing a drop point.

        This returns a JSON representation of the dict constructed by
        :meth:`get_dp_info()`.
        """
        return json.dumps({number: cls.get_dp_info(number)}, indent=indent)

    @staticmethod
    def get_dps_json(
        time: datetime = None, indent: Optional[int] = 4 if app.debug else None
    ) -> str:
        """
        Get drop points as a JSON string.

        If a time has been given as optional parameters, only drop points
        are returned that have changes since that time stamp, i.e. have
        been created, visited, reported or changed their location.
        """

        if time is None:
            dps = DropPoint.query.all()
        else:
            dp_set = set()
            dp_set.update(
                [dp for dp in DropPoint.query.filter(DropPoint.time > time).all()],
                [loc.dp for loc in Location.query.filter(Location.time > time).all()],
                [vis.dp for vis in Visit.query.filter(Visit.time > time).all()],
                [rep.dp for rep in Report.query.filter(Report.time > time).all()],
            )
            dps = list(dp_set)

        ret = {}

        for dp in dps:
            ret[dp.number] = DropPoint.get_dp_info(dp.number)

        return json.dumps(ret, indent=indent)

    @staticmethod
    def get_next_free_number() -> int:
        """
        Get the next free drop point number.
        """
        last = DropPoint.query.order_by(desc(DropPoint.number)).limit(1).first()
        if last:
            return last.number + 1
        else:
            return 1

    def __repr__(self) -> str:
        return f"Drop point {self.number} ({'inactive' if self.removed else 'active'})"
