package JyCSP;

import org.python.core.*;

public class Seq extends PyObject implements JyCspSeqInterface{
	
	protected JyCspSeqInterface jcsi;
	
	public Seq(JavaCspProcess...args){
		super();
		this.jcsi = CspFactory.createJavaCspSeq(args);
	}

	public void run() {
		this.jcsi.run();
	}

	public void start() {
		this.jcsi.start();
	}

	public void stop() {
		this.jcsi.stop();
	}

}