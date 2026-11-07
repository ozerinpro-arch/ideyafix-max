"""Small durable receipts for user turns and structured model responses.

No provider, prompt, draft or external I/O belongs here. A model call that was
started but lost its response is never repeated automatically after a crash.
"""
import hashlib
import json

from fastapi import HTTPException


class ConversationStore:
    def __init__(self, connection, *, initialize=True):
        self.connection = connection
        if not initialize:
            return
        connection.execute("""CREATE TABLE IF NOT EXISTS schedule_agent_turns (
            task_id TEXT NOT NULL, turn INTEGER NOT NULL, request_hash TEXT NOT NULL,
            request_json TEXT NOT NULL, model_started INTEGER NOT NULL DEFAULT 0,
            model_json TEXT, result_json TEXT, PRIMARY KEY(task_id,turn)
        )""")
        connection.commit()

    def get(self, task_id, turn):
        row = self.connection.execute(
            "SELECT request_hash,request_json,model_started,model_json,result_json FROM schedule_agent_turns WHERE task_id=? AND turn=?",
            (task_id, turn),
        ).fetchone()
        return dict(zip(("request_hash", "request_json", "model_started", "model_json", "result_json"), row)) if row else None

    def reserve(self, task_id, turn, payload, *, commit=True):
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        row = self.get(task_id, turn)
        if row:
            if row["request_hash"] != digest:
                raise HTTPException(409, "Для этого шага уже сохранён другой ответ")
            return row
        self.connection.execute(
            "INSERT INTO schedule_agent_turns(task_id,turn,request_hash,request_json) VALUES(?,?,?,?)",
            (task_id, turn, digest, encoded),
        )
        if commit:
            self.connection.commit()
        return self.get(task_id, turn)

    def model_response(self, task_id, turn, invoke):
        row = self.get(task_id, turn)
        if row is None:
            raise RuntimeError("User turn was not reserved")
        if row["model_json"] is not None:
            return json.loads(row["model_json"])
        if row["model_started"]:
            raise RuntimeError("Previous model response was interrupted; automatic retry is disabled")
        self.connection.execute("UPDATE schedule_agent_turns SET model_started=1 WHERE task_id=? AND turn=?", (task_id, turn))
        self.connection.commit()
        response = invoke()
        self.connection.execute("UPDATE schedule_agent_turns SET model_json=? WHERE task_id=? AND turn=?", (json.dumps(response, ensure_ascii=False), task_id, turn))
        self.connection.commit()
        return response

    def finish(self, task_id, turn, result):
        self.connection.execute(
            "UPDATE schedule_agent_turns SET result_json=COALESCE(result_json,?) WHERE task_id=? AND turn=?",
            (json.dumps(result, ensure_ascii=False), task_id, turn),
        )
        self.connection.commit()

    def delete_task(self, task_id):
        self.connection.execute("DELETE FROM schedule_agent_turns WHERE task_id=?", (task_id,))
        self.connection.commit()
