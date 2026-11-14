"""File picker integration."""

from ._support import (
    _shared, bpy,
)

class Browse:
    """File browser behind GPU File Path fields; each GPU UI Root registers one subclass with the properties."""

    def invoke(self, context, event):
        pending = _shared.get("_gpu_browse_pending")
        if not pending:
            return {"CANCELLED"}
        self.filter_glob = pending["pattern"] or "*"
        if pending["value"]:
            path = bpy.path.abspath(pending["value"])
            if pending["folder"]:
                self.directory = path
            else:
                self.filepath = path
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        pending = _shared.pop("_gpu_browse_pending", None)
        if pending:
            path = self.directory if pending["folder"] else self.filepath
            if pending["relative"] and bpy.data.filepath:
                try:
                    path = bpy.path.relpath(path)
                except ValueError:
                    pass  # Windows paths on another drive cannot be relative.
            pending["overlay"]._browse_done(pending["key"], path)
        return {"FINISHED"}

    def cancel(self, context):
        _shared.pop("_gpu_browse_pending", None)

