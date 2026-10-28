# 索引与回收机制

## 数据采集与持久化

`windows_native.py` 使用 `FileIdExtdDirectoryRestartInfo` / `FileIdExtdDirectoryInfo` 读取 64 KiB 的目录记录，直接得到文件大小、三类时间和目录 ID。接口不可用时使用 `os.scandir`。目录联接及其他重解析点不进入遍历。

SQLite 以根目录与格式版本生成独立数据库名称，保存相对路径，在查询结果中还原绝对路径。首次批量写入后再建立辅助索引，减少建库时的维护开销。月份统计单独保存，读取概览无需扫描文件表。

完整更新使用事务和采集代次。取消、异常或失败会回滚；无法读取的子树保留上次记录并显示错误。文件被移出时只把索引记录标为不可用，不操作源文件，也保留旧记录。索引采用 PERSIST 回滚日志，临时排序在内存完成。

## 增量更新

目录通知在首次采集之前开始监听。单个文件的新增或修改只读取该文件的当前元数据；目录变化只核对变更目录与新增、变化的子目录。未变化的相邻子树不会被反复扫描。

USN 接口只读取既有日志。文件 ID 与目录 ID 用于确定属于所选根目录的事件。日志 ID、游标、记录边界或版本不再可信时重新建立完整基线；通知缓冲区溢出也完整核验，以免漏掉只修改文件内容的事件。

普通权限不能读取日志时，运行期间仍使用目录通知，关闭期间按目录时间变化补齐新增、移出和重命名。只修改已有文件内容且未改变目录时间的离线变化，需要用户主动重新核验。这是目录增量模式的明确限制。

## 主线程与预览

SQLite 查询、元数据采集和图片解码在工作线程完成。Tk 控件与 `ImageTk.PhotoImage` 只在主线程创建和更新。回调队列先安排下一次调度，再执行当前回调，使 `wait_window` 弹窗中的缩略图仍能显示。

每次分页有独立代次，过时的查询或解码结果不再显示。预览只保存当前页的缩略图，窗口关闭后丢弃异步结果。框选事件绑定到预览窗口，不使用全局鼠标绑定。

## 文件计划与回收

确认前生成具体的文件计划，记录根目录、原因、全部绝对路径、大小、文件 ID、时间及可选截止月份。日期候选再按当前文件时间过滤，防止依据旧索引选择已变为更晚日期的文件；关闭程序期间修改已有文件后，仍应先完整核验，以免遗漏新符合条件的文件。

确认对象包含计划 ID 和摘要，不能用于其他文件列表。执行前及 `PreDeleteItem` 回调中再次核验目标：它必须是根目录内的同一普通文件，不经过链接或联接，大小和时间仍与核对时一致。

Windows 回收站策略和剩余容量须可核验，关闭回收站、未知容量或容量不足均停止。回收使用 `IFileOperation` 的回收标志、早停策略和进度保护回调。回调拒绝永久删除路径，并要求系统返回回收目标。没有 `os.remove`、`unlink` 或永久删除回退。

本次验证只测试保护条件和回调，并通过真实 COM 完成文件绑定、保护回调注册和排队。未调用真实 `PerformOperations`，没有回收用户文件。

参考：[GetFileInformationByHandleEx](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getfileinformationbyhandleex)、[ReadDirectoryChangesW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-readdirectorychangesw)、[IFileOperation 操作标志](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nf-shobjidl_core-ifileoperation-setoperationflags)。
