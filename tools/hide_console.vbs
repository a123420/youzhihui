Set sh = CreateObject("Wscript.Shell")
If WScript.Arguments.Count < 1 Then WScript.Quit 1
bat = WScript.Arguments(0)
Set fso = CreateObject("Scripting.FileSystemObject")
sh.CurrentDirectory = fso.GetParentFolderName(bat)
q = Chr(34)
code = sh.Run("cmd /c " & q & q & bat & q & " run" & q, 0, True)
If code <> 0 Then
  sh.Run "cmd /c " & q & q & bat & q & " show" & q, 1, True
End If
