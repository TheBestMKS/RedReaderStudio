using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;
[assembly: AssemblyTitle("RedReaderStudio")]
[assembly: AssemblyDescription("Offline text-to-speech project studio")]
[assembly: AssemblyVersion("0.7.0.0")]
[assembly: AssemblyFileVersion("0.7.0.0")]
class Launcher {
    [STAThread]
    static void Main(string[] args) {
        var root = AppDomain.CurrentDomain.BaseDirectory;
        var runtime = Path.Combine(root, "runtime", "pythonw.exe");
        var script = Path.Combine(root, "app", "launch.py");
        try {
            if (!File.Exists(runtime) || !File.Exists(script))
                throw new Exception("Перенесите всю папку программы, включая runtime, app и models.");
            var arguments = "\"" + script + "\"";
            if (Array.IndexOf(args, "--smoke-test") >= 0) arguments += " --smoke-test";
            if (Array.IndexOf(args, "--api") >= 0) arguments += " --api";
            var portIndex = Array.IndexOf(args, "--port");
            if (portIndex >= 0) {
                int port;
                if (portIndex + 1 >= args.Length || !Int32.TryParse(args[portIndex + 1], out port) || port < 1024 || port > 65535)
                    throw new Exception("Порт должен быть числом от 1024 до 65535.");
                arguments += " --port " + port;
            }
            var info = new ProcessStartInfo(runtime, arguments);
            info.WorkingDirectory = root;
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.EnvironmentVariables.Remove("PYTHONHOME");
            info.EnvironmentVariables.Remove("PYTHONPATH");
            Process.Start(info);
        } catch (Exception error) {
            MessageBox.Show(error.Message, "RedReaderStudio", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
