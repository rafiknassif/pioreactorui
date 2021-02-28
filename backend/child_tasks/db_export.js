const { execFile } = require("child_process");


process.on('message', function(options) {
    tables = Object.keys(options.datasetCheckbox)
    tables = tables.filter(t => options.datasetCheckbox[t])
    experiment = options.experimentSelection
    cmd_tables = tables.map(s => ["--tables", s]).flat()
    filename = `export_${experiment.replace(" ", "_")}_${Math.floor(Date.now() / 1000).toString()}.zip`
    console.log(["run", "download_experiment_data", "--experiment", `"${experiment}"`, "--output", `"/home/pi/pioreactorui/backend/build/static/exports/${filename}"`].concat(cmd_tables))
    execFile("pio", ["run", "download_experiment_data", "--experiment", `"${experiment}"`, "--output", `"/home/pi/pioreactorui/backend/build/static/exports/${filename}"`].concat(cmd_tables), (error, stdout, stderr) => {
        console.log(stdout)
        console.log(stderr)
        if (error) {
            console.log(error)
            process.send(false);
        } else {
            process.send(filename);
        }
        process.exit(0)
    });
});