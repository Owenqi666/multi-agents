"""Find the newest integration for a delegated stage's exact frozen members."""

from multi_agent.core.integration import IntegrationRecord

from .integration_store import IntegrationStore, _record

type IntegrationMemberVersion = tuple[str, int, str]


class ProjectIntegrationStore(IntegrationStore):
    def newest_matching_integration(
        self,
        repository: str,
        baseline: str,
        members: tuple[IntegrationMemberVersion, ...],
        *,
        integration_id: str | None = None,
    ) -> IntegrationRecord | None:
        expected = sorted(members)
        if integration_id is None:
            rows = self._connection.execute(
                """SELECT * FROM integrations WHERE repository = ?
                   ORDER BY (target_baseline = ?) DESC, rowid DESC""",
                (repository, baseline),
            )
        else:
            rows = self._connection.execute(
                """SELECT * FROM integrations WHERE integration_id = ?
                   AND repository = ? AND target_baseline = ?""",
                (integration_id, repository, baseline),
            )
        for row in rows:
            record = _record(row)
            actual = sorted(
                (member.task_run_id, member.attempt, member.delivery.candidate)
                for member in record.members
            )
            if actual == expected:
                return record
        return None
