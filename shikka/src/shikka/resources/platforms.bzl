"""Host compilation with optional cache access; worker deployment is separate."""
def _execution(ctx):
    enabled = read_root_config("shikka", "cache", "off") != "off"
    executor = CommandExecutorConfig(
        local_enabled = True,
        remote_enabled = False,
        remote_cache_enabled = enabled,
        allow_cache_uploads = enabled and read_root_config("shikka", "cache_write", "false") == "true",
        remote_execution_properties = {
            "OSFamily": read_root_config("shikka", "os"),
            "ISA": read_root_config("shikka", "arch"),
            "shikka.toolchain": read_root_config("shikka", "toolchain"),
        },
        remote_execution_use_case = "shikka",
        remote_output_paths = "output_paths",
    )
    return [DefaultInfo(), ExecutionPlatformRegistrationInfo(platforms = [ExecutionPlatformInfo(
        label = ctx.label.raw_target(), configuration = ConfigurationInfo(constraints = {}, values = {}), executor_config = executor)])]

execution = rule(impl = _execution, attrs = {})
